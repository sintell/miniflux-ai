"""Opt-in real fork + isolated PostgreSQL; all article/digest LLMs are mocked.

Requires MINIFLUX_METADATA_TEST_BINARY and TEST_META_DATABASE_URL. The caller owns
an empty disposable loopback database ending in _test (see docs/metadata.md).
"""
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from core.generate_daily_news import generate_daily_news
from core.process_entries import process_entry
from services.editorial import ARTICLE_PROMPT, DIGEST_PROMPT
from services.miniflux_client import Client, MetadataAPIError, OUTPUT_FIELDS
from services.state import State


class FixtureFeed(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/article'):
            self.server.article_headers = dict(self.headers)
            self.server.article_requests = getattr(self.server, 'article_requests', 0) + 1
            body = ('''<html><body><nav>Excluded navigation</nav><div id="story"><p>
            OLD_TERM introduces a caching mechanism for repeated requests. The publisher
            reports lower latency in its own benchmark, but independent measurements are
            not available. Existing deployments can enable the cache explicitly and
            retain the documented fallback behavior.</p></div><aside>Excluded sidebar</aside>
            </body></html>''').encode()
            content_type = 'text/html'
        else:
            body = f'''<rss version="2.0"><channel><title>Local fixture</title>
            <link>http://127.0.0.1:{self.server.server_port}/</link><description>Local fixture</description>
            </channel></rss>'''.encode()
            content_type = 'application/rss+xml'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('MINIFLUX_METADATA_TEST_BINARY'), 'Opt-in isolated fork/PostgreSQL test')
class ForkIntegrationTests(unittest.TestCase):
    def test_real_api_and_crash_replay_preserve_native_data_and_other_namespaces(self):
        dsn = os.environ.get('TEST_META_DATABASE_URL', '')
        database = urlsplit(dsn)
        if database.hostname not in ('127.0.0.1', 'localhost') or not database.path.endswith('_test'):
            self.fail('Integration requires a disposable loopback PostgreSQL database ending in _test')
        binary = Path(os.environ['MINIFLUX_METADATA_TEST_BINARY'])
        if not binary.is_file():
            self.fail('Build the local fork binary first')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {'PATH': os.environ.get('PATH', ''), 'DATABASE_URL': dsn,
               'LISTEN_ADDR': f'127.0.0.1:{port}', 'RUN_MIGRATIONS': '1', 'CREATE_ADMIN': '1',
               'ADMIN_USERNAME': 'companion-test', 'ADMIN_PASSWORD': 'companion-test-only',
               'FETCHER_ALLOW_PRIVATE_NETWORKS': '1', 'POLLING_FREQUENCY': '99999',
               'SCHEDULER_ENTRY_FREQUENCY_MAX_INTERVAL': '99999'}
        server = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop_server():
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        self.addCleanup(stop_server)
        url = f'http://127.0.0.1:{port}'
        admin = Client(url, username='companion-test', password='companion-test-only', timeout=2, retries=0)
        self.addCleanup(admin.close)
        for _ in range(100):
            if server.poll() is not None:
                self.fail('Isolated fork startup failed; check test database and build/configuration')
            try:
                admin.list_metadata()
                break
            except MetadataAPIError:
                time.sleep(0.1)
        else:
            self.fail('Isolated fork did not become ready')
        key = admin.create_api_key(f'companion isolated test {time.time_ns()}')['token']
        client = Client(url, api_key=key, retries=0)
        self.addCleanup(client.close)
        fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureFeed)
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        def stop_fixture():
            fixture.shutdown()
            fixture.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop_fixture)
        fixture_url = f'http://127.0.0.1:{fixture.server_port}/feed'
        category_id = client.get_categories()[0]['id']
        feed_id = client.create_feed(fixture_url, category_id=category_id)
        article_url = f'http://127.0.0.1:{fixture.server_port}/article'
        imported = client.import_entry(feed_id, article_url, title='Original title',
                                       content='<p>Original content</p>', tags=['React', 'TypeScript'], status='unread')
        entry_id = imported['id']
        original = client.get_entry(entry_id)
        self.assertNotIn('meta', original)
        client.update_feed(feed_id, scraper_rules='#story', rewrite_rules='replace("OLD_TERM"|"RULEAPPLIED")',
                           user_agent='companion-rule-fixture', cookie='fixture_cookie=yes')
        preview = client.fetch_entry_content(entry_id, retries=0)
        self.assertIn('RULEAPPLIED', preview['content'])
        self.assertNotIn('OLD_TERM', preview['content'])
        self.assertNotIn('Excluded navigation', preview['content'])
        self.assertNotIn('Excluded sidebar', preview['content'])
        self.assertEqual(fixture.article_headers.get('User-Agent'), 'companion-rule-fixture')
        self.assertEqual(fixture.article_headers.get('Cookie'), 'fixture_cookie=yes')
        self.assertFalse('X-Auth-Token' in fixture.article_headers)
        self.assertFalse('Authorization' in fixture.article_headers)
        after_preview = client.get_entry(entry_id)
        for field in ('title', 'content', 'tags', 'changed_at'):
            self.assertEqual(after_preview[field], original[field], field)
        client.ensure_metadata()
        self.assertEqual(client.get_entry(entry_id, meta=OUTPUT_FIELDS)['meta'], {})
        definition = client.register_metadata('summary', 'Description should not be replaced')
        self.assertNotEqual(definition['description'], 'Description should not be replaced')
        client.register_metadata('note', 'Omitted field preservation fixture')
        client.patch_metadata(entry_id, {'note': 'preserved'})
        client._request('POST', '/meta/register', 'fixture_register', expected=(200, 201),
                        json={'client_id': 'other_app', 'name': 'status', 'description': 'Fixture'})
        client._request('PATCH', f'/entries/{entry_id}/meta/other_app', 'fixture_write', expected=(204,),
                        json={'status': 'reviewed'})
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = SimpleNamespace(
            state_path=Path(temp.name) / 'digest.sqlite3', enrichment_mode='metadata',
            language='English', article_prompt=ARTICLE_PROMPT, digest_prompt=DIGEST_PROMPT,
            relevance_criteria='TEST ONLY: relevance to software engineering.', metadata_input_fields=[],
            llm_max_length=None, agents={'summary': {'title': 'AI summary:', 'style_block': True}},
            editorial={'article_words_min': 80, 'article_words_max': 130, 'max_records': 100,
                       'max_input_chars': 60000, 'max_stories': 8, 'max_groups': 3,
                       'at_a_glance_words': 75, 'story_words_min': 40, 'story_words_max': 70},
            timezone='UTC', editions={'morning': '05:00', 'evening': '12:00', 'night': '21:00'},
            weather={'enabled': False, 'max_age_minutes': 30}, miniflux_browser_url=url,
            ai_news_url=fixture_url,
            article_fetch={'mode': 'thin', 'min_feed_chars': 500, 'min_article_chars': 200,
                           'timeout': 30, 'max_bytes': 2097152, 'max_text_chars': 20000, 'retries': 0})
        raw = json.dumps({'summary': 'A fixture summary.', 'rating': '8.5', 'topics': ['Ignored inference']})
        with patch('core.process_entries.get_ai_result', return_value=raw) as llm:
            with patch.object(State, 'complete_enrichment', side_effect=OSError('Simulated crash')):
                with self.assertRaises(OSError):
                    process_entry(client, original, config)
            state = State(config.state_path)
            self.assertEqual(state.queued(), [])
            self.assertEqual(client.get_entry(entry_id)['changed_at'], original['changed_at'])
            client.update_entries([entry_id], 'read')
            changed_at_after_status = client.get_entry(entry_id)['changed_at']
            process_entry(client, original, config)
            process_entry(client, original, config)
            self.assertEqual(llm.call_count, 1)
            model_input = json.loads(llm.call_args.args[1])
            self.assertEqual(model_input['source']['kind'], 'miniflux_article')
            self.assertIn('RULEAPPLIED', model_input['content'])
            self.assertNotIn('Excluded sidebar', model_input['content'])
            self.assertEqual(fixture.article_requests, 2)  # Manual preview + initial generation; retries do not refetch.
        enriched = client.get_entry(entry_id, meta=['*'])
        for field in ('id', 'title', 'content', 'tags', 'url'):
            self.assertEqual(enriched[field], original[field], field)
        self.assertEqual(enriched['changed_at'], changed_at_after_status)
        self.assertEqual(enriched['meta'], {
            'miniflux_ai.note': 'preserved', 'other_app.status': 'reviewed',
            'miniflux_ai.rating': '8.5', 'miniflux_ai.summary': 'A fixture summary.',
            'miniflux_ai.topics': '["React","TypeScript"]'})
        before = copy.deepcopy(enriched['meta'])
        with self.assertRaises(MetadataAPIError) as rejected:
            client.patch_metadata(entry_id, {'summary': 'Must not be written', 'unregistered': 'value'})
        self.assertEqual(rejected.exception.status_code, 400)
        self.assertEqual(rejected.exception.category, 'unregistered_field')
        self.assertEqual(client.get_entry(entry_id, meta=['*'])['meta'], before)
        with self.assertRaises(MetadataAPIError) as missing:
            client.patch_metadata(999999999, {'summary': 'Missing article'})
        self.assertEqual(missing.exception.category, 'entry_not_found')
        denied = Client(url, api_key='invalid-test-key', retries=0)
        self.addCleanup(denied.close)
        with self.assertRaises(MetadataAPIError) as unauthorized:
            denied.list_metadata()
        self.assertEqual(unauthorized.exception.category, 'authentication')
        projected = client.get_entries(meta=['miniflux_ai.summary', 'other_app.status'])
        projected_entry = next(entry for entry in projected['entries'] if entry['id'] == entry_id)
        self.assertEqual(projected_entry['meta'], {'miniflux_ai.summary': 'A fixture summary.', 'other_app.status': 'reviewed'})
        self.assertEqual(state.queued()[0]['summary'], 'A fixture summary.')
        digest_response = json.dumps({
            'title': 'Fixture digest', 'opening': 'A fixture opening.', 'at_a_glance': 'A development.',
            'labels': {'at_a_glance': 'At a glance', 'stories': 'Stories', 'source': 'Source', 'original_article': 'Original article'},
            'stories': [{'headline': 'Fixture headline', 'body': 'Fixture body.', 'source_ids': [f'entry:{entry_id}'], 'group': ''}]})
        weather = Mock(return_value=None)
        digest = generate_daily_news(client, config, llm=Mock(return_value=digest_response), weather_fetcher=weather,
                                     now=datetime(2026, 10, 11, 7, 30, tzinfo=timezone.utc))
        self.assertIsNotNone(digest)
        self.assertIn(f'/feed/{feed_id}/entry/{entry_id}', digest['html'])
        self.assertIn(original['url'], digest['html'])
        self.assertEqual(state.queued(), [])
