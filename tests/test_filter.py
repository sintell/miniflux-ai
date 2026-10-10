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

    def test_metadata_accepts_publisher_quotes_and_preserves_selection(self):
        agent = {'title': 'AI summary:', 'style_block': True}
        config = SimpleNamespace(enrichment_mode='metadata', agents={'summary': agent})
        quote = '<blockquote><p>A publisher opening quotation.</p></blockquote><p>Article body.</p>'
        for site, content, settings, expected in [
            ('https://dev.to', quote, {}, True),
            ('https://dev.to', quote, {'allow_list': ['https://dev.*']}, True),
            ('https://dev.to', quote, {'allow_list': ['https://elsewhere.com']}, False),
            ('https://dev.to', quote, {'deny_list': ['https://dev.*']}, False),
            ('https://dev.to', '<!-- miniflux-ai -->' + quote, {}, False),
            ('https://dev.to', 'AI summary: previously processed', {}, False),
            ('https://ai-news.miniflux', quote, {}, False),
            ('https://feeds-status.miniflux', quote, {}, False),
        ]:
            with self.subTest(site=site, content=content, settings=settings):
                entry = {'content': content, 'feed': {'site_url': site}}
                self.assertEqual(filter_entry(config, ('summary', agent | settings), entry), expected)
