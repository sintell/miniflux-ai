import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from core.fetch_unread_entries import fetch_unread_entries
from core.get_ai_result import build_messages
from core.process_entries import process_entry
from services.editorial import ARTICLE_PROMPT, GenerationError
from services.enrichment import validate_enrichment
from services.miniflux_client import Client, DEFINITIONS, MetadataAPIError, NAMESPACE, OUTPUT_FIELDS
from services.state import State


def response(status=200, value=None):
    result = Mock(status_code=status)
    result.json.return_value = value
    return result


def generated(**changes):
    return json.dumps({'summary': 'A localized summary.', 'rating': '8.5', 'topics': ['Tools', 'React']} | changes)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.client = Client('http://localhost:12345', api_key='test-only', session=self.session, retries=2)

    def test_registration_payload_and_canonical_response_201_and_200(self):
        for status in (201, 200):
            canonical = {'qualified_name': 'miniflux_ai.summary', 'description': 'Original preserved description'}
            self.session.request.return_value = response(status, canonical)
            self.assertEqual(self.client.register_metadata('summary', 'New description'), canonical)
            args, kwargs = self.session.request.call_args
            self.assertEqual(args, ('POST', 'http://localhost:12345/v1/meta/register'))
            self.assertEqual(kwargs['json'], {'client_id': NAMESPACE, 'name': 'summary', 'description': 'New description'})

    def test_patch_is_namespace_scoped_strings_and_204_is_not_decoded(self):
        reply = response(204)
        reply.json.side_effect = AssertionError('204 must not decode JSON')
        self.session.request.return_value = reply
        values = {'rating': '8.5', 'summary': 'Text', 'topics': '["React"]'}
        self.client.patch_metadata(123, values)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ('PATCH', 'http://localhost:12345/v1/entries/123/meta/miniflux_ai'))
        self.assertEqual(json.loads(kwargs['data']), values)
        self.assertTrue(all(isinstance(value, str) for value in json.loads(kwargs['data']).values()))
        reply.json.assert_not_called()
        self.session.request.reset_mock()
        with self.assertRaises(MetadataAPIError):
            self.client.patch_metadata(123, {'rating': 8.5})
        self.session.request.assert_not_called()

    def test_repeated_projection_query_and_raw_entry_fields_survive(self):
        entries = [
            {'id': 1, 'title': 'Publisher', 'tags': ['React']},
            {'id': 2, 'tags': [], 'meta': {}},
            {'id': 3, 'tags': ['TypeScript'], 'meta': {'another_app.status': 'reviewed', 'miniflux_ai.summary': ''}},
        ]
        self.session.request.return_value = response(200, {'total': 3, 'entries': entries})
        page = self.client.get_entries()
        projected_entries = copy.deepcopy(entries)
        projected_entries[0]['meta'] = {}
        self.session.request.return_value = response(200, {'total': 3, 'entries': projected_entries})
        projected = self.client.get_entries(meta=['miniflux_ai.summary', 'another_app.status'])
        kwargs = self.session.request.call_args.kwargs
        prepared = requests.Request('GET', 'http://localhost/v1/entries', params=kwargs['params']).prepare()
        self.assertEqual(parse_qs(urlsplit(prepared.url).query)['meta'], ['miniflux_ai.summary', 'another_app.status'])
        self.assertEqual(page['entries'], entries)
        self.assertEqual(projected['entries'][0]['meta'], {})
        self.assertNotIn('meta', page['entries'][0])
        self.assertEqual(page['entries'][1]['meta'], {})
        self.assertNotIn('miniflux_ai.rating', page['entries'][2]['meta'])
        self.assertEqual(page['entries'][2]['meta']['miniflux_ai.summary'], '')
        self.session.request.return_value = response(200, entries[2])
        self.assertEqual(self.client.get_entry(3, meta=['another_app.status']), entries[2])
        self.assertEqual(self.session.request.call_args.kwargs['params'], {'meta': ['another_app.status']})
        self.client.get_entry(3)
        self.assertIsNone(self.session.request.call_args.kwargs['params'])

    def test_projection_ignored_by_server_is_an_incompatibility_diagnostic(self):
        self.session.request.return_value = response(200, {'total': 1, 'entries': [{'id': 123}]})
        with self.assertRaises(MetadataAPIError) as caught:
            self.client.get_entries(meta=OUTPUT_FIELDS)
        self.assertEqual(caught.exception.category, 'invalid_response')

    def test_known_write_outage_blocks_further_generation_readiness(self):
        self.session.request.side_effect = self.readiness_reply
        self.client.ensure_metadata()
        self.session.request.side_effect = None
        self.session.request.return_value = response(503)
        with patch('services.miniflux_client.time.sleep'), self.assertRaises(MetadataAPIError):
            self.client.patch_metadata(123, {'summary': 'Text'})
        before = self.session.request.call_count
        with self.assertRaises(MetadataAPIError):
            self.client.ensure_metadata()
        self.assertEqual(self.session.request.call_count, before)
        self.session.request.side_effect = self.readiness_reply
        self.client.ensure_metadata(force=True)

    def test_atomic_rejection_and_authentication_categories_are_safe(self):
        for status, category in ((400, 'unregistered_field'), (401, 'authentication'),
                                 (403, 'access'), (404, 'entry_not_found')):
            self.session.request.return_value = response(status, {'error_message': "Metadata field 'miniflux_ai.bad' is not registered PRIVATE"})
            with self.assertRaises(MetadataAPIError) as caught:
                self.client.patch_metadata(123, {'summary': 'PRIVATE TEXT', 'rating': '8.5'})
            error = caught.exception
            self.assertEqual(error.category, category)
            self.assertFalse(error.retryable)
            self.assertEqual(error.status_code, status)
            self.assertEqual(error.fields, ('miniflux_ai.summary', 'miniflux_ai.rating'))
            self.assertNotIn('PRIVATE', str(error))
            self.session.request.reset_mock()

    def test_transient_retries_are_bounded(self):
        for failure in (response(429), response(503), requests.ConnectionError('PRIVATE CREDENTIALS')):
            self.session.request.reset_mock()
            if isinstance(failure, Exception):
                self.session.request.side_effect = failure
            else:
                self.session.request.side_effect = None
                self.session.request.return_value = failure
            with patch('services.miniflux_client.time.sleep') as sleep:
                with self.assertRaises(MetadataAPIError) as caught:
                    self.client.patch_metadata(123, {'summary': 'Text'})
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertEqual(self.session.request.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_incompatible_and_malformed_registry_block_readiness(self):
        for reply, expected in ((response(404), 'incompatible'), (response(405), 'incompatible'),
                                (response(200, {}), 'invalid_response')):
            client = Client('http://localhost:12345', api_key='test-only', session=self.session)
            self.session.request.return_value = reply
            self.session.request.reset_mock()
            with self.assertRaises(MetadataAPIError) as caught:
                client.ensure_metadata()
            self.assertEqual(caught.exception.category, expected)
            self.assertFalse(caught.exception.retryable)
            with self.assertRaises(MetadataAPIError):
                client.ensure_metadata()
            self.assertEqual(self.session.request.call_count, 1)

    def readiness_reply(self, method, url, **kwargs):
        if url.endswith('/meta'):
            return response(200, [])
        if url.endswith('/meta/register'):
            body = kwargs['json']
            return response(201, body | {'qualified_name': f"{body['client_id']}.{body['name']}"})
        if method == 'PATCH':
            self.assertEqual(kwargs['json'], {})
            return response(204)
        return response(200, {'total': 1, 'entries': [{'id': 123, 'meta': {}}]})

    def test_concurrent_readiness_registers_once_then_force_refreshes(self):
        self.session.request.side_effect = self.readiness_reply
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: self.client.ensure_metadata(['another_app.status']), range(8)))
        self.assertEqual(self.session.request.call_count, 6)  # list, three register, projection, empty PATCH
        calls = self.session.request.call_args_list
        self.assertEqual([call.kwargs['json']['name'] for call in calls[1:4]], list(DEFINITIONS))
        self.assertEqual(calls[4].kwargs['params']['meta'], OUTPUT_FIELDS + ['another_app.status'])
        self.client.ensure_metadata(['another_app.status'], force=True)
        self.assertEqual(self.session.request.call_count, 12)
        fresh = Client('http://localhost:12345', api_key='different-user', session=self.session)
        fresh.ensure_metadata()
        self.assertEqual(self.session.request.call_count, 18)

    def test_registry_failure_invalidates_positive_cache(self):
        self.session.request.side_effect = self.readiness_reply
        self.client.ensure_metadata()
        self.session.request.side_effect = None
        self.session.request.return_value = response(400, {'error_message': 'Metadata field is not registered'})
        with self.assertRaises(MetadataAPIError):
            self.client.patch_metadata(123, {'summary': 'Text'})
        self.session.request.side_effect = self.readiness_reply
        before = self.session.request.call_count
        self.client.ensure_metadata()
        self.assertEqual(self.session.request.call_count - before, 6)

    def test_missing_write_route_is_detected_by_noop_before_generation(self):
        def reply(method, url, **kwargs):
            return response(405) if method == 'PATCH' else self.readiness_reply(method, url, **kwargs)
        self.session.request.side_effect = reply
        with self.assertRaises(MetadataAPIError) as caught:
            self.client.ensure_metadata()
        self.assertEqual(caught.exception.operation, 'verify_write')
        self.assertEqual(caught.exception.category, 'incompatible')


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = SimpleNamespace(
            state_path=Path(self.temp.name) / 'digest.sqlite3', enrichment_mode='metadata',
            language='German', article_prompt=ARTICLE_PROMPT,
            editorial={'article_words_min': 80, 'article_words_max': 130}, llm_max_length=None,
            relevance_criteria='TEST POLICY: prefer documented database engineering.',
            metadata_input_fields=['selected.context'], llm_max_workers=2,
            agents={'summary': {'title': 'AI summary:', 'style_block': True},
                    'translate': {'title': 'AI translation:', 'style_block': False}})
        self.entry = {'id': 123, 'title': 'Original title', 'content': '<p>Original content</p>',
                      'tags': [' TypeScript ', 'React', 'React'], 'url': 'https://example.com/article',
                      'feed': {'id': 4, 'title': 'Publisher', 'site_url': 'https://example.com'},
                      'meta': {'selected.context': 'Useful context', 'private_app.secret': 'PRIVATE', 'miniflux_ai.summary': 'Old partial'}}
        self.client = Mock()
        self.client.get_entry.return_value = copy.deepcopy(self.entry)
        self.state = State(self.config.state_path)

    def process(self):
        process_entry(self.client, self.entry, self.config)

    def test_language_tags_selected_input_and_native_fields_are_preserved(self):
        original = copy.deepcopy(self.client.get_entry.return_value)
        def llm(prompt, request, **kwargs):
            self.assertIn('German', prompt)
            self.assertIn(self.config.relevance_criteria, prompt)
            self.assertTrue(kwargs['structured'])
            data = json.loads(request)
            self.assertEqual(data['title'], self.entry['title'])
            self.assertEqual(data['tags'], ['React', 'TypeScript'])
            self.assertEqual(data['meta'], {'selected.context': 'Useful context'})
            messages = build_messages(prompt, request, self.config, structured=True)
            self.assertIn('untrusted data', messages[0]['content'])
            self.assertIn('Useful context', messages[1]['content'])
            self.assertNotIn('PRIVATE', request)
            return generated(topics=['Wrong inferred label'])
        with patch('core.process_entries.get_ai_result', side_effect=llm):
            self.process()
        self.client.ensure_metadata.assert_called_once_with(['selected.context'])
        self.client.get_entry.assert_called_once_with(123, meta=OUTPUT_FIELDS + ['selected.context'])
        names = [call[0] for call in self.client.mock_calls]
        self.assertLess(names.index('ensure_metadata'), names.index('get_entry'))
        self.assertLess(names.index('verify_metadata_write'), names.index('patch_metadata'))
        values = self.client.patch_metadata.call_args.args[1]
        self.assertEqual(values['topics'], '["React","TypeScript"]')
        self.assertEqual(values['rating'], '8.5')
        self.client.update_entry.assert_not_called()
        self.client.update_entries.assert_not_called()
        self.assertEqual(self.client.get_entry.return_value, original)
        self.assertEqual(self.state.get_enrichment(123)['status'], 'complete')
        record = self.state.queued()[0]
        self.assertEqual((record['entry_id'], record['feed_id'], record['url']), (123, 4, self.entry['url']))
        self.assertEqual(record['summary'], 'A localized summary.')

    def test_missing_tags_infers_deterministic_topics(self):
        self.client.get_entry.return_value.pop('tags')
        with patch('core.process_entries.get_ai_result', return_value=generated(topics=['Tools', ' React ', 'Tools'])):
            self.process()
        self.assertEqual(self.client.patch_metadata.call_args.args[1]['topics'], '["React","Tools"]')

    def test_invalid_generated_responses_never_persist_or_complete(self):
        invalid = ['not JSON', generated(rating=8.5), generated(rating='8'), generated(rating='0.9'),
                   generated(rating='10.1'), generated(rating='8.50'), generated(rating=' 8.5'),
                   generated(rating='NaN'), generated(summary=''), generated(topics=[1]),
                   generated(summary='contains\x00NUL'), generated(extra='unexpected')]
        for raw in invalid:
            with self.subTest(raw=raw), patch('core.process_entries.get_ai_result', return_value=raw):
                with self.assertRaises(GenerationError):
                    self.process()
            self.assertIsNone(self.state.get_enrichment(123))
            self.assertEqual(self.state.queued(), [])
        self.client.patch_metadata.assert_not_called()
        for rating in ('1.0', '9.9', '10.0'):
            self.assertEqual(validate_enrichment(generated(rating=rating), [])['rating'], rating)
        with self.assertRaises(GenerationError):
            validate_enrichment(generated(topics=[]), [])

    def test_failed_batch_retains_result_and_retry_after_restart_avoids_llm(self):
        self.client.patch_metadata.side_effect = MetadataAPIError('patch_metadata', 503, entry_id=123)
        with patch('core.process_entries.get_ai_result', return_value=generated()) as llm:
            with self.assertRaises(MetadataAPIError):
                self.process()
            self.assertEqual(self.state.queued(), [])
            self.assertEqual(self.state.get_enrichment(123)['status'], 'pending')
            self.client.patch_metadata.side_effect = None
            # A new State object is created by processing each time, as on restart.
            self.process()
            self.assertEqual(llm.call_count, 1)
        self.assertEqual(self.client.get_entry.call_count, 1)
        self.assertEqual(self.client.patch_metadata.call_count, 2)
        self.assertEqual(len(self.state.queued()), 1)

    def test_remote_write_local_commit_crash_window_replays_same_values(self):
        with patch('core.process_entries.get_ai_result', return_value=generated()) as llm:
            with patch.object(State, 'complete_enrichment', side_effect=OSError('PRIVATE')):
                with self.assertRaises(OSError):
                    self.process()
            self.assertEqual(self.state.queued(), [])
            self.process()
            self.process()
            self.assertEqual(llm.call_count, 1)
        calls = self.client.patch_metadata.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(self.state.queued()), 1)

    def test_concurrent_processing_reuses_local_result(self):
        with patch('core.process_entries.get_ai_result', return_value=generated()) as llm:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(lambda _: self.process(), range(8)))
            self.assertEqual(llm.call_count, 1)
        self.assertEqual(self.client.patch_metadata.call_count, 1)
        self.assertEqual(len(self.state.queued()), 1)

    def test_unsupported_api_does_not_generate_or_fallback_and_logs_safe_reason(self):
        self.client.ensure_metadata.side_effect = MetadataAPIError('list_definitions', 404, category='incompatible')
        with patch('core.process_entries.get_ai_result') as llm, self.assertLogs('miniflux_ai', 'ERROR') as logs:
            with self.assertRaises(MetadataAPIError):
                self.process()
        llm.assert_not_called()
        self.client.update_entry.assert_not_called()
        self.client.patch_metadata.assert_not_called()
        self.assertIn('Deploy the metadata-enabled Miniflux fork', '\n'.join(logs.output))
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))

    def test_404_retains_outbox_as_unavailable_not_successful(self):
        self.client.patch_metadata.side_effect = MetadataAPIError('patch_metadata', 404, entry_id=123)
        with patch('core.process_entries.get_ai_result', return_value=generated()):
            with self.assertRaises(MetadataAPIError):
                self.process()
        self.assertEqual(self.state.get_enrichment(123)['status'], 'unavailable')
        self.assertEqual(self.state.pending_enrichments(), [])
        self.assertEqual(self.state.queued(), [])

    def test_400_atomic_failure_has_no_digest_record_and_retries_saved_batch(self):
        self.client.patch_metadata.side_effect = MetadataAPIError('patch_metadata', 400, entry_id=123)
        with patch('core.process_entries.get_ai_result', return_value=generated()) as llm:
            with self.assertRaises(MetadataAPIError):
                self.process()
            self.assertEqual(self.state.queued(), [])
            self.assertEqual(self.state.get_enrichment(123)['error_category'], 'validation_or_registration')
            self.client.patch_metadata.side_effect = None
            self.process()
            self.assertEqual(llm.call_count, 1)

    def test_legacy_records_and_injected_content_are_not_migrated(self):
        old = self.state.enqueue({'entry_id': 123, 'summary': 'Old summary'})
        self.state.publish({'id': 'old', 'generated_at': '2026-10-01T07:30:00+00:00',
                            'edition': 'morning', 'title': 'Old digest', 'html': '<p>Old</p>'}, [old['record_id']])
        with patch('core.process_entries.get_ai_result') as llm:
            self.process()
            self.client.get_entry.return_value['id'] = 124
            self.client.get_entry.return_value['content'] = '<!-- miniflux-ai -->Old injection<p>Original</p>'
            process_entry(self.client, {'id': 124}, self.config)
        llm.assert_not_called()
        self.client.patch_metadata.assert_not_called()
        self.client.update_entry.assert_not_called()
        self.assertEqual(self.state.get_record(123)['summary'], 'Old summary')
        self.assertEqual(self.state.queued(), [])
        self.assertEqual(self.state.digests()[0]['id'], 'old')

    def test_unrelated_metadata_does_not_prevent_discovery_or_completion(self):
        self.client.get_entry.return_value['meta'] = {'private_app.secret': 'PRIVATE'}
        with patch('core.process_entries.get_ai_result', return_value=generated()):
            self.process()
        self.assertEqual(self.state.get_enrichment(123)['status'], 'complete')

    def test_pagination_has_no_output_filter_and_retries_read_entries(self):
        self.state.save_enrichment(122, {'summary': 'Saved', 'rating': '8.5', 'topics': '["Tools"]'},
                                   {'entry_id': 122, 'summary': 'Saved'})
        self.client.get_entries.side_effect = [
            {'total': 3, 'entries': [{'id': 123}, {'id': 124}]},
            {'total': 3, 'entries': [{'id': 125}]}]
        with patch('core.fetch_unread_entries.process_entry') as process:
            fetch_unread_entries(self.config, self.client)
        self.client.ensure_metadata.assert_called_once_with(['selected.context'], force=True)
        ids = [call.args[1]['id'] for call in process.call_args_list]
        self.assertEqual(ids[0], 122)
        self.assertEqual(sorted(ids), [122, 123, 124, 125])
        self.assertEqual([call.kwargs['offset'] for call in self.client.get_entries.call_args_list], [0, 2])
        for call in self.client.get_entries.call_args_list:
            self.assertNotIn('filter', call.kwargs)
            self.assertNotIn('changed_after', call.kwargs)
            self.assertEqual(call.kwargs['status'], ['unread'])
            self.assertEqual(call.kwargs['meta'], OUTPUT_FIELDS + ['selected.context'])
