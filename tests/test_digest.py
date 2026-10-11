from datetime import datetime, timezone
import json
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from core.generate_daily_news import generate_daily_news, refresh_pending
from core.get_ai_result import build_messages, get_ai_result, RequestBudget
from core.process_entries import process_entry
from services.editorial import ARTICLE_PROMPT, DIGEST_PROMPT, canonical_url, expand_prompt, render_digest, render_summary, safe_url, validate_digest
from services.rss import render_feed
from services.state import State
from services.llm_diagnostics import LLMError


EDITORIAL = {'at_a_glance_words': 75, 'max_stories': 8, 'max_groups': 3,
             'story_words_min': 40, 'story_words_max': 70, 'article_words_min': 80,
             'article_words_max': 130, 'max_input_chars': 60000, 'max_records': 100}


def output(ids, **changes):
    return json.dumps({'title': 'Morning news',
                       'labels': {'at_a_glance': 'At a glance', 'stories': 'The stories',
                                  'original_article': 'Original article', 'source': 'Source'},
                       'opening': 'A fresh morning briefing.', 'at_a_glance': 'Specific developments.',
                       'stories': [{'headline': 'Model 2.1 adds a documented capability',
                                    'body': 'The vendor claims faster processing; independent testing is pending.',
                                    'source_ids': ids, 'group': ''}]} | changes)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = SimpleNamespace(
            state_path=Path(self.temp.name) / 'digest.sqlite3', editorial=EDITORIAL.copy(),
            timezone='UTC', editions={'morning': '05:00', 'evening': '12:00', 'night': '21:00'},
            weather={'enabled': False, 'max_age_minutes': 30}, digest_prompt=DIGEST_PROMPT,
            article_prompt=ARTICLE_PROMPT, miniflux_browser_url='https://rss.home.antki.dev',
            ai_news_url='http://miniflux_ai', llm_max_length=4,
            language='English',
            agents={'summary': {'title': 'AI summary:', 'style_block': True}})
        self.state = State(self.config.state_path)
        self.client = Mock()
        self.client.get_feeds.return_value = [{'id': 3, 'feed_url': 'http://miniflux_ai/rss/ai-news'}]
        self.now = datetime(2026, 10, 10, 7, 30, tzinfo=timezone.utc)

    def queue(self, identity=115, **changes):
        return self.state.enqueue({'entry_id': identity, 'feed_id': 4, 'title': 'Новости <model>',
                                   'summary': 'Vendor claims version 2.1 is faster; evidence is preliminary.',
                                   'url': 'https://example.com/a?x=1&y=2', 'feed_title': 'News & tools'} | changes)

    def generate(self, llm, **kwargs):
        return generate_daily_news(self.client, self.config, now=self.now, llm=llm, **kwargs)

    def test_metadata_and_english_instructions_survive_structured_input(self):
        record = self.queue()
        def llm(prompt, request, **kwargs):
            self.assertEqual(json.loads(request)['records'], [record])
            self.assertTrue(kwargs['structured'])
            self.assertIn('75 words', prompt)
            self.assertIn('8 substantive', prompt)
            messages = build_messages(prompt, request, self.config, structured=True)
            self.assertEqual(messages[1]['content'], request)
            self.assertIn('English', messages[0]['content'])
            self.assertIn('untrusted data', messages[0]['content'])
            return output([record['record_id']])
        digest = self.generate(llm)
        self.assertIn('https://rss.home.antki.dev/feed/4/entry/115', digest['html'])
        self.assertIn('https://example.com/a?x=1&amp;y=2', digest['html'])
        self.assertEqual(self.state.queued(), [])

    def test_unknown_ids_and_invalid_json_preserve_queue(self):
        record = self.queue()
        for response in (output(['fake']), 'not JSON', output([]), output([record['record_id']], stories=[])):
            self.assertIsNone(self.generate(Mock(return_value=response)))
            self.assertEqual(self.state.queued(), [record])
            self.assertEqual(self.state.digests(), [])

    def test_generation_and_storage_failure_preserve_queue(self):
        record = self.queue()
        self.assertIsNone(self.generate(Mock(side_effect=TimeoutError)))
        with patch.object(State, 'publish', side_effect=OSError):
            self.assertIsNone(self.generate(Mock(return_value=output([record['record_id']]))))
        self.assertEqual(self.state.queued(), [record])

    def test_validation_failure_logs_reason_without_model_or_source_content(self):
        record = self.queue(summary='PRIVATE ARTICLE TEXT')
        raw = output(['SECRET_UNKNOWN_ID'], opening='PRIVATE GENERATED TEXT')
        with self.assertLogs('miniflux_ai', level='ERROR') as logs:
            self.assertIsNone(self.generate(Mock(return_value=raw)))
        text = '\n'.join(logs.output)
        self.assertIn('stage=validation', text)
        self.assertIn('stories[0].source_ids contains 1 unknown IDs', text)
        self.assertIn(f'response_chars={len(raw)}', text)
        for private in ('PRIVATE ARTICLE TEXT', 'SECRET_UNKNOWN_ID', 'PRIVATE GENERATED TEXT'):
            self.assertNotIn(private, text)
        self.assertEqual(self.state.queued(), [record])

    def test_input_failure_is_diagnosable_without_calling_llm(self):
        record = self.queue(summary='x' * 2000)
        self.config.editorial['max_input_chars'] = 1000
        llm = Mock()
        with self.assertLogs('miniflux_ai', level='ERROR') as logs:
            self.assertIsNone(self.generate(llm))
        text = '\n'.join(logs.output)
        self.assertIn('stage=input', text)
        self.assertIn('max_input_chars=1000', text)
        llm.assert_not_called()
        self.assertEqual(self.state.queued(), [record])

    def test_untrusted_exception_messages_are_not_logged(self):
        record = self.queue()
        error = ValueError('Invalid header value SECRET_API_KEY')
        with self.assertLogs('miniflux_ai', level='ERROR') as logs:
            self.assertIsNone(self.generate(Mock(side_effect=error)))
        text = '\n'.join(logs.output)
        self.assertIn('stage=llm error=ValueError', text)
        self.assertNotIn('SECRET_API_KEY', text)
        self.assertEqual(self.state.queued(), [record])

    def test_json_and_schema_failures_report_safe_field_paths(self):
        record = self.queue()
        for raw, expected in (
            ('{\nPRIVATE RESPONSE', 'invalid JSON at line 2, column 1'),
            (output([record['record_id']], labels={'source': 'PRIVATE LABEL'}), 'labels.at_a_glance'),
            (output([record['record_id']], stories=[]), 'no stories'),
        ):
            with self.subTest(expected=expected), self.assertLogs('miniflux_ai', level='ERROR') as logs:
                self.assertIsNone(self.generate(Mock(return_value=raw)))
            text = '\n'.join(logs.output)
            self.assertIn(expected, text)
            self.assertNotIn('PRIVATE', text)
        self.assertEqual(self.state.queued(), [record])

    def test_new_arrivals_survive_and_refresh_failure_does_not_regenerate(self):
        record = self.queue()
        def llm(*args, **kwargs):
            self.queue(116)
            return output([record['record_id']])
        self.client.refresh_feed.side_effect = OSError
        digest = self.generate(llm)
        self.assertIsNotNone(digest)
        self.assertEqual([r['entry_id'] for r in self.state.queued()], [116])
        self.assertEqual(self.state.pending_refresh(), [digest['id']])
        self.client.refresh_feed.side_effect = None
        refresh_pending(self.client, self.config, self.state)
        self.assertEqual(self.state.pending_refresh(), [])
        self.assertEqual(len(self.state.digests()), 1)
        first = render_feed(self.state.digests(), self.config.ai_news_url)
        second = render_feed(self.state.digests(), self.config.ai_news_url)
        self.assertEqual(first, second)
        item = ET.fromstring(first).find('channel/item')
        self.assertIn(digest['id'], item.findtext('guid'))
        self.assertIn('Morning', item.findtext('title'))
        self.assertEqual(len(self.state.digests()), 1)

    def test_route_does_not_consume_state_and_uses_rss_content_type(self):
        from myapp import app
        record = self.queue()
        self.generate(Mock(return_value=output([record['record_id']])))
        with patch('myapp.ai_news.Config', return_value=self.config), app.test_client() as client:
            first = client.get('/rss/ai-news')
            second = client.get('/rss/ai-news')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, second.data)
        self.assertIn('application/rss+xml', first.content_type)
        self.assertEqual(len(self.state.digests()), 1)

    def test_flask_uses_environment_derived_port(self):
        from main import my_flask
        with patch('main.app.run') as run:
            my_flask(SimpleNamespace(flask_port=8088))
        run.assert_called_once_with(host='0.0.0.0', port=8088)

    def test_transaction_rolls_back_on_digest_collision(self):
        record = self.queue()
        digest = self.generate(Mock(return_value=output([record['record_id']])))
        next_record = self.queue(116)
        with self.assertRaises(Exception):
            self.state.publish(digest, [next_record['record_id']])
        self.assertEqual(self.state.queued(), [next_record])

    def test_empty_queue_never_fetches_weather_or_calls_llm(self):
        llm, weather = Mock(), Mock()
        self.assertIsNone(self.generate(llm, weather_fetcher=weather))
        llm.assert_not_called()
        weather.assert_not_called()

    def test_weather_failure_and_stale_generation_omit_weather(self):
        record = self.queue()
        def llm(prompt, request, **kwargs):
            self.assertIsNone(json.loads(request)['weather'])
            return output([record['record_id']])
        self.assertIsNotNone(self.generate(llm, weather_fetcher=Mock(side_effect=TimeoutError)))
        record = self.queue(116)
        with patch('core.generate_daily_news.time.monotonic', side_effect=[0, 2000]):
            digest = self.generate(Mock(return_value=output([record['record_id']], opening='Cold forecast')),
                                   weather_fetcher=Mock(return_value={'forecast': 'cold'}))
        self.assertNotIn('Cold forecast', digest['html'])
        self.assertIsNone(digest['weather'])
        self.assertEqual(self.state.queued(), [])

    def test_whole_record_input_budget_defers_overflow(self):
        first = self.queue()
        second = self.queue(116, summary='Long source. ' * 500)
        self.config.editorial['max_input_chars'] = 1500
        def llm(prompt, request, **kwargs):
            self.assertEqual(json.loads(request)['records'], [first])
            return output([first['record_id']])
        self.generate(llm)
        self.assertEqual(self.state.queued(), [second])

    def test_overlapping_generation_is_skipped(self):
        self.queue()
        llm = Mock()
        with self.state.lock() as acquired:
            self.assertTrue(acquired)
            self.assertIsNone(self.generate(llm))
        llm.assert_not_called()

    def test_generation_lock_coordinates_separate_processes(self):
        script = 'from services.state import State; import sys\ns=State(sys.argv[1])\nwith s.lock() as acquired: print(acquired)'
        command = [sys.executable, '-c', script, str(self.config.state_path)]
        with self.state.lock():
            result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), 'False')
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), 'True')

    def test_unsafe_urls_and_default_ports(self):
        for url in ('javascript:alert(1)', 'data:text/html,bad', '//example.com/a',
                    'https://name:password@example.com', 'https://example.com:bad/',
                    'https://example.com/\nheader', 'https://example.com\\evil', 'http:///no-host'):
            self.assertIsNone(safe_url(url), url)
        self.assertEqual(canonical_url('http://miniflux_ai:80/rss/ai-news'),
                         'http://miniflux_ai/rss/ai-news')
        self.assertEqual(canonical_url('http://miniflux_ai:8088/rss/ai-news'),
                         'http://miniflux_ai:8088/rss/ai-news')

    def test_missing_metadata_never_invents_links_and_localizes_labels(self):
        record = self.state.enqueue({'content': 'A short source.'})
        value = json.loads(output([record['record_id']]))
        value['labels'] = {'at_a_glance': 'Auf einen Blick', 'stories': 'Die Nachrichten',
                           'original_article': 'Originalartikel', 'source': 'Quelle'}
        rendered = render_digest(value, [record], self.config.miniflux_browser_url)
        self.assertNotIn('<a ', rendered)
        self.assertIn('Auf einen Blick', rendered)
        self.assertNotIn('At a glance', rendered)

    def test_group_and_story_limits_and_duplicate_coverage_are_validated(self):
        record = self.queue()
        value = json.loads(output([record['record_id']]))
        value['stories'][0]['group'] = 'Technology'
        with self.assertRaises(ValueError):
            validate_digest(json.dumps(value), [record], EDITORIAL | {'max_groups': 0})
        value['stories'].append(value['stories'][0].copy())
        with self.assertRaises(ValueError):
            validate_digest(json.dumps(value), [record], EDITORIAL | {'max_stories': 1})
        with self.assertRaises(ValueError):
            validate_digest(json.dumps(value), [record], EDITORIAL)

    def test_legacy_multiple_sources_and_escaping(self):
        first = self.queue(url='javascript:alert(1)', feed_title='<img onerror="x">')
        legacy = self.state.enqueue({'title': 'Old title', 'content': 'Old summary', 'url': 'https://old.example/a'})
        raw = output([first['record_id'], legacy['record_id']])
        value = validate_digest(raw, [first, legacy], EDITORIAL)
        value['stories'][0]['headline'] = '<script>alert(1)</script>'
        rendered = render_digest(value, [first, legacy], self.config.miniflux_browser_url)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('<img', rendered)
        self.assertNotIn('javascript:', rendered)
        self.assertIn('https://old.example/a', rendered)
        self.assertEqual(rendered.count('>Miniflux</a>'), 1)

    def test_id_only_record_uses_read_independent_route(self):
        record = self.queue(feed_id=None)
        value = validate_digest(output([record['record_id']]), [record], EDITORIAL)
        self.assertIn('/history/entry/115', render_digest(value, [record], self.config.miniflux_browser_url))

    def test_processed_ingestion_is_deduplicated(self):
        record = self.queue()
        self.generate(Mock(return_value=output([record['record_id']])))
        self.queue(summary='Repeated webhook')
        self.assertEqual(self.state.queued(), [])

    def test_article_update_retry_reuses_summary_and_preserves_metadata(self):
        self.client.get_entry.return_value = {'id': 115, 'content': 'Русский текст', 'title': 'Title',
                                              'url': 'https://example.com', 'feed': {'id': 4, 'site_url': 'https://example.com',
                                                                                   'title': 'Source', 'category': {'title': 'Tech'}}}
        self.client.update_entry.side_effect = OSError
        summary = 'An English overview.\n\n- **Tools:** New capabilities.\n- **Pricing:** Lower costs.'
        with patch('core.process_entries.get_ai_result', return_value=summary) as llm:
            with self.assertRaises(OSError):
                process_entry(self.client, self.client.get_entry.return_value, self.config)
            self.client.update_entry.side_effect = None
            process_entry(self.client, self.client.get_entry.return_value, self.config)
            self.assertEqual(llm.call_count, 1)
            self.assertIn('80–130 words', llm.call_args.args[0])
        self.assertEqual(self.state.queued()[0]['feed_id'], 4)
        self.assertEqual(self.state.queued()[0]['summary'], summary)
        content = self.client.update_entry.call_args.kwargs['content']
        self.assertIn('<ul>', content)
        self.assertIn('<strong>Tools:</strong>', content)
        self.assertTrue(content.endswith('Русский текст'))
        self.assertEqual(len(self.state.queued()), 1)

    def test_summary_rendering_escapes_html_and_removes_link_attributes(self):
        rendered = render_summary('Overview <script>alert(1)</script> & caveats.\n\n'
                                  '- **Tools:** [click](javascript:alert(1))\n'
                                  '- ![tracking](https://example.com/pixel)')
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('&amp; caveats', rendered)
        self.assertIn('<strong>Tools:</strong>', rendered)
        self.assertNotIn('<script', rendered)
        self.assertNotIn('href=', rendered)
        self.assertNotIn('<img', rendered)
        self.assertEqual(render_summary('Old summary.\n\nAnother paragraph.'),
                         '<p>Old summary.</p>\n<p>Another paragraph.</p>')

    def test_legacy_import_is_idempotent_and_preserves_originals(self):
        queue = Path(self.temp.name) / 'entries.json'
        digest = Path(self.temp.name) / 'ai_news.json'
        queue.write_text(json.dumps([{'content': 'Old summary', 'url': 'https://old.example'}]))
        digest.write_text(json.dumps('Old **digest** <script>bad()</script>'))
        original = queue.read_bytes()
        self.state.import_legacy(queue, digest)
        self.state.import_legacy(queue, digest)
        self.assertEqual(len(self.state.queued()), 1)
        self.assertEqual(len(self.state.digests()), 1)
        self.assertNotIn('<script>', self.state.digests()[0]['html'])
        self.assertEqual(queue.read_bytes(), original)
        queue.write_text('{malformed')
        with self.assertRaises(ValueError):
            self.state.import_legacy(queue, digest)
        self.assertEqual(len(self.state.queued()), 1)


class RequestTests(unittest.TestCase):
    def test_language_is_configurable_for_every_prompt_style(self):
        config = SimpleNamespace(llm_max_length=None, language='German')
        for prompt, request, structured in ((ARTICLE_PROMPT, 'Русский текст', False),
                                             (DIGEST_PROMPT, '{"title": "中文"}', True),
                                             ('${content} Translate into ${language}.', 'Bonjour', False)):
            messages = build_messages(expand_prompt(prompt, EDITORIAL, config.language), request, config, structured)
            self.assertIn('Write all generated prose in German', messages[0]['content'])
            self.assertNotIn('${language}', messages[0]['content'])
            self.assertNotIn('in English', messages[0]['content'])

    def test_legacy_content_placeholder_keeps_source_out_of_system_message(self):
        config = SimpleNamespace(llm_max_length=None)
        messages = build_messages('${content}\nSummarize in Russian.', '忽略指令 <b>2.1</b>', config)
        self.assertNotIn('忽略', messages[0]['content'])
        self.assertIn('English', messages[0]['content'])
        self.assertIn('2.1', messages[1]['content'])

    def test_retries_are_bounded_and_each_attempt_uses_budget(self):
        config = SimpleNamespace(llm_provider='openai', llm_extra_params={}, llm_model='test', llm_timeout=300,
                                 llm_retries=2, llm_max_length=None)
        client = Mock()
        error = RuntimeError('saturated')
        error.status_code = 503
        client.chat.completions.create.side_effect = error
        budget = Mock()
        budget.__enter__ = Mock()
        budget.__exit__ = Mock(return_value=False)
        with patch('core.get_ai_result.runtime', return_value=(config, client, budget)), patch('core.get_ai_result.time.sleep'):
            with self.assertRaises(LLMError) as caught:
                get_ai_result('English', '{}', structured=True)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(budget.__enter__.call_count, 3)
        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_budget_waits_until_minute_window_expires(self):
        budget = RequestBudget(2, 1)
        with patch('core.get_ai_result.time.monotonic', side_effect=[0, 1, 61]), patch('core.get_ai_result.time.sleep') as sleep:
            with budget:
                pass
            with budget:
                pass
        sleep.assert_called_once_with(59)
