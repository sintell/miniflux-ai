import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.process_entries import process_entry
from services.article_source import DEFAULTS, feed_text_length, linked_source
from services.editorial import ARTICLE_PROMPT
from services.enrichment import generation_input
from services.miniflux_client import Client, MetadataAPIError
from services.state import State


ARTICLE = ('The release introduces a caching mechanism for repeated requests. '
           'The publisher reports lower latency in its own benchmark, while independent '
           'measurements are not yet available. Existing deployments can enable the new '
           'cache explicitly and retain the documented fallback behavior. ')
STUB = ('<p>Article URL: <a href="https://example.com/article">https://example.com/article</a></p>'
        '<p>Comments URL: <a href="https://news.ycombinator.com/item?id=123">Discussion</a></p>'
        '<p>Points: 32</p><p># Comments: 13</p>')


class ArticleSourceTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(article_fetch=dict(DEFAULTS), llm_max_length=None, metadata_input_fields=[])
        self.entry = {'id': 123, 'title': 'Fixture', 'url': 'https://example.com/article', 'content': STUB}
        self.client = Mock()
        self.client.fetch_entry_content.return_value = {'content': '<article><p>' + ARTICLE + '</p></article>', 'reading_time': 1}

    def test_thin_detection_ignores_hn_counters_and_urls(self):
        self.assertEqual(feed_text_length(STUB), 0)
        self.assertGreater(feed_text_length('<p>' + ARTICLE * 3 + '</p>'), 500)

    def test_full_feed_and_disabled_mode_do_not_fetch(self):
        self.assertIsNone(linked_source(self.client, self.entry | {'content': '<p>' + ARTICLE * 3 + '</p>'}, self.config))
        self.config.article_fetch['mode'] = 'off'
        self.assertIsNone(linked_source(self.client, self.entry, self.config))
        self.client.fetch_entry_content.assert_not_called()

    def test_native_preview_keeps_feed_rules_on_server(self):
        source = linked_source(self.client, self.entry, self.config)
        self.assertEqual(source['kind'], 'miniflux_article')
        self.assertIn('caching mechanism', source['content'])
        self.assertEqual(source['url'], self.entry['url'])
        self.client.fetch_entry_content.assert_called_once_with(123, timeout=30, retries=1, max_bytes=2097152)
        self.client.get_feed.assert_not_called()  # No copying rule/cookie/password config.

    def test_always_fetches_and_short_preview_uses_limited_context(self):
        self.config.article_fetch['mode'] = 'always'
        linked_source(self.client, self.entry | {'content': ARTICLE * 3}, self.config)
        self.assertEqual(self.client.fetch_entry_content.call_count, 1)
        for content in ('', STUB, '<p>Short</p>'):
            self.client.fetch_entry_content.return_value = {'content': content}
            with self.subTest(content=content):
                source = linked_source(self.client, self.entry, self.config)
                self.assertEqual(source['kind'], 'limited_context')
                self.assertEqual(source['reason'], 'insufficient_article_text')
                if content == '<p>Short</p>':
                    self.assertEqual(source['content'], 'Short')

    def test_retrieval_failures_fall_back_but_api_contract_and_access_errors_do_not(self):
        for category in ('transport', 'temporary', 'response_too_large'):
            with self.subTest(category=category):
                self.client.fetch_entry_content.side_effect = MetadataAPIError('fetch_content', category=category)
                source = linked_source(self.client, self.entry, self.config)
                self.assertEqual(source['kind'], 'limited_context')
                self.assertEqual(source['reason'], category)
        for category in ('authentication', 'access', 'entry_not_found', 'invalid_response', 'unexpected_response'):
            with self.subTest(category=category):
                self.client.fetch_entry_content.side_effect = MetadataAPIError('fetch_content', category=category)
                with self.assertRaises(MetadataAPIError):
                    linked_source(self.client, self.entry, self.config)

    def test_feed_only_input_includes_title_url_and_feed_identity(self):
        self.entry['content'] = ''
        self.entry['feed'] = {'title': 'News', 'site_url': 'https://example.com'}
        request = json.loads(generation_input(self.entry, self.config))
        self.assertEqual(request['title'], 'Fixture')
        self.assertEqual(request['url'], self.entry['url'])
        self.assertEqual(request['feed']['title'], 'News')
        self.assertEqual(request['feed']['site_url'], 'https://example.com')
        self.assertEqual(request['content'], '')

    def test_limits_and_source_provenance_do_not_mutate_entry(self):
        self.config.article_fetch['max_text_chars'] = 220
        self.config.llm_max_length = 100
        original = copy.deepcopy(self.entry)
        source = linked_source(self.client, self.entry, self.config)
        self.assertEqual(len(source['content']), 220)
        self.assertTrue(source['truncated'])
        request = json.loads(generation_input(self.entry, self.config, source))
        self.assertEqual(len(request['content']), 100)
        self.assertTrue(request['source']['truncated'])
        self.assertEqual(request['source']['kind'], 'miniflux_article')
        self.assertIn('Comments URL', request['feed_content'])
        self.assertEqual(self.entry, original)


class PreviewClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        self.client = Client('http://localhost:12345', api_key='test-only', session=self.session)

    def reply(self, status=200, body=None):
        reply = Mock(status_code=status)
        data = json.dumps(body or {'content': ARTICLE, 'reading_time': 1}).encode()
        reply.iter_content.return_value = [data]
        self.session.request.return_value = reply
        return reply

    def test_fetch_explicitly_disables_storage_and_bounds_response(self):
        reply = self.reply()
        result = self.client.fetch_entry_content(123, timeout=17, retries=0, max_bytes=1024)
        self.assertEqual(result['content'], ARTICLE)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ('GET', 'http://localhost:12345/v1/entries/123/fetch-content'))
        self.assertEqual(kwargs['params'], {'update_content': 'false'})
        self.assertEqual(kwargs['timeout'], 17)
        self.assertTrue(kwargs['stream'])
        reply.close.assert_called_once()
        reply.json.assert_not_called()

    def test_oversized_or_invalid_preview_is_rejected(self):
        reply = self.reply()
        with self.assertRaises(MetadataAPIError) as caught:
            self.client.fetch_entry_content(123, max_bytes=100)
        self.assertEqual(caught.exception.category, 'response_too_large')
        reply.close.assert_called_once()
        for body in ({'content': None}, {'reading_time': 1}, {'content': 123}, []):
            self.reply(body=body or {'content': None})
            with self.assertRaises(MetadataAPIError) as caught:
                self.client.fetch_entry_content(123)
            self.assertEqual(caught.exception.category, 'invalid_response')

    def test_preview_retries_are_bounded_without_invalidating_metadata_readiness(self):
        self.client._ready_until = 999999999
        self.reply(status=503)
        with patch('services.miniflux_client.time.sleep') as sleep, self.assertRaises(MetadataAPIError) as caught:
            self.client.fetch_entry_content(123, retries=1)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.session.request.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(self.client._ready_until, 999999999)
        self.session.request.reset_mock()
        self.reply(status=404)
        with self.assertRaises(MetadataAPIError) as caught:
            self.client.fetch_entry_content(123, retries=1)
        self.assertEqual(caught.exception.category, 'entry_not_found')
        self.assertEqual(self.session.request.call_count, 1)


class ArticleProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = SimpleNamespace(
            state_path=Path(self.temp.name) / 'digest.sqlite3', enrichment_mode='metadata',
            article_fetch=dict(DEFAULTS), language='English', llm_max_length=None,
            metadata_input_fields=[], relevance_criteria='Fixture policy', article_prompt=ARTICLE_PROMPT,
            editorial={'article_words_min': 80, 'article_words_max': 130},
            agents={'summary': {'title': 'AI summary:', 'style_block': True}})
        self.entry = {'id': 123, 'title': 'Fixture release', 'content': STUB, 'tags': ['React'],
                      'url': 'https://example.com/article', 'meta': {}, 'feed': {'id': 4}}
        self.client = Mock()
        self.client.get_entry.return_value = self.entry
        self.client.fetch_entry_content.return_value = {'content': '<p>' + ARTICLE + '</p>'}
        self.raw = json.dumps({'summary': 'A substantive article summary.', 'rating': '8.5', 'topics': ['Tools']})

    def test_native_article_drives_generation_without_updating_publisher(self):
        original = copy.deepcopy(self.entry)
        with patch('core.process_entries.get_ai_result', return_value=self.raw) as llm:
            process_entry(self.client, self.entry, self.config)
        request = json.loads(llm.call_args.args[1])
        self.assertIn('caching mechanism', request['content'])
        self.assertEqual(request['source']['kind'], 'miniflux_article')
        self.assertEqual(request['tags'], ['React'])
        self.assertIn("Miniflux's own feed scraper/rewrite rules", llm.call_args.args[0])
        self.client.update_entry.assert_not_called()
        self.assertEqual(self.entry, original)
        record = State(self.config.state_path).queued()[0]
        self.assertEqual((record['entry_id'], record['feed_id'], record['url']), (123, 4, self.entry['url']))
        self.assertEqual(record['summary'], 'A substantive article summary.')

    def test_authentication_failure_avoids_llm_and_completion(self):
        self.client.fetch_entry_content.side_effect = MetadataAPIError('fetch_content', 401, entry_id=123)
        with patch('core.process_entries.get_ai_result') as llm, self.assertLogs('miniflux_ai', 'ERROR') as logs:
            with self.assertRaises(MetadataAPIError):
                process_entry(self.client, self.entry, self.config)
        llm.assert_not_called()
        self.client.patch_metadata.assert_not_called()
        self.assertIn('stage=fetch_source', '\n'.join(logs.output))
        self.assertNotIn('https://example.com', '\n'.join(logs.output))
        self.assertEqual(State(self.config.state_path).queued(), [])
        self.assertIsNone(State(self.config.state_path).get_enrichment(123))

    def test_limited_sources_still_generate_and_persist_rating(self):
        for entry_id, preview in enumerate(('', STUB, '<p>Yandex Cloud</p>', None), start=200):
            with self.subTest(preview=preview):
                entry = copy.deepcopy(self.entry)
                entry['id'] = entry_id
                entry['title'] = '3rd Yandex Cloud Data Center Was Hit'
                self.client.get_entry.return_value = entry
                self.client.fetch_entry_content.side_effect = (
                    MetadataAPIError('fetch_content', 500, entry_id=entry['id']) if preview is None else None)
                self.client.fetch_entry_content.return_value = {'content': preview}
                with patch('core.process_entries.get_ai_result', return_value=self.raw) as llm:
                    process_entry(self.client, entry, self.config)
                request = json.loads(llm.call_args.args[1])
                self.assertEqual(request['source']['kind'], 'limited_context')
                self.assertEqual(request['source']['reason'], 'temporary' if preview is None else 'insufficient_article_text')
                self.assertEqual(request['title'], entry['title'])
                self.assertEqual(request['url'], entry['url'])
                self.assertIn('Article URL', request['feed_content'])
                if preview == '<p>Yandex Cloud</p>':
                    self.assertEqual(request['content'], 'Yandex Cloud')
                saved = State(self.config.state_path).get_enrichment(entry['id'])
                self.assertEqual(saved['status'], 'complete')
                self.assertEqual(saved['values']['rating'], '8.5')
                self.client.patch_metadata.assert_called_with(entry['id'], saved['values'])
                self.client.update_entry.assert_not_called()

    def test_saved_retry_and_completed_article_avoid_fetch_and_regeneration(self):
        self.client.patch_metadata.side_effect = OSError
        with patch('core.process_entries.get_ai_result', return_value=self.raw) as llm:
            with self.assertRaises(OSError):
                process_entry(self.client, self.entry, self.config)
            self.client.patch_metadata.side_effect = None
            process_entry(self.client, self.entry, self.config)
            process_entry(self.client, self.entry, self.config)
            self.client.fetch_entry_content.assert_called_once()
            llm.assert_called_once()
        self.assertEqual(State(self.config.state_path).get_enrichment(123)['status'], 'complete')
