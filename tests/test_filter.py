from types import SimpleNamespace
import unittest
from core.entry_filter import filter_entry


class FilterTests(unittest.TestCase):
    def test_allow_deny_processed_and_internal_feeds(self):
        agent = {'title': 'AI summary:', 'style_block': True}
        config = SimpleNamespace(agents={'summary': agent})
        for site, content, settings, expected in [
            ('https://example.com', 'source', {}, True),
            ('https://example.com', 'source', {'allow_list': ['https://elsewhere.com']}, False),
            ('https://example.com', 'source', {'allow_list': ['https://example.*']}, True),
            ('https://example.com', 'source', {'deny_list': ['https://example.*']}, False),
            ('https://example.com', '<!-- miniflux-ai -->source', {}, False),
            ('https://example.com', '<blockquote>old summary', {}, False),
            ('https://ai-news.miniflux', 'source', {}, False),
            ('https://feeds-status.miniflux', 'source', {}, False),
        ]:
            with self.subTest(site=site, content=content, settings=settings):
                entry = {'content': content, 'feed': {'site_url': site}}
                self.assertEqual(filter_entry(config, ('summary', agent | settings), entry), expected)
