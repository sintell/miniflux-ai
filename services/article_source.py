"""Read-only article retrieval through Miniflux's own feed-rule pipeline."""
import re

from bs4 import BeautifulSoup
from markdownify import markdownify

from common.logger import logger
from services.miniflux_client import MetadataAPIError


DEFAULTS = {'mode': 'thin', 'min_feed_chars': 500, 'min_article_chars': 200,
            'timeout': 30, 'max_bytes': 2 * 1024 * 1024, 'max_text_chars': 20000,
            'retries': 1}


def limited_source(entry, settings, reason, content=''):
    text = markdownify(content).strip()
    limit = settings['max_text_chars']
    logger.info('operation=fetch_article entry_id=%s source=limited_context reason=%s', entry['id'], reason)
    return {'kind': 'limited_context', 'url': entry.get('url', ''),
            'content': text[:limit], 'truncated': len(text) > limit, 'reason': reason}


def feed_text_length(content):
    soup = BeautifulSoup(content, 'html.parser')
    for element in soup(['script', 'style', 'noscript', 'template']):
        element.decompose()
    for paragraph in soup.find_all('p'):
        text = paragraph.get_text(' ', strip=True)
        if re.match(r'^(Article URL:|Comments URL:|Points:|#\s*Comments:)', text):
            paragraph.decompose()
    for anchor in soup.find_all('a'):
        anchor.decompose()
    text = re.sub(r'https?://\S+', '', soup.get_text(' ', strip=True))
    return len(' '.join(text.split()))


def linked_source(client, entry, config):
    settings = getattr(config, 'article_fetch', {'mode': 'off'})
    mode = settings['mode']
    if mode == 'off' or mode == 'thin' and feed_text_length(entry.get('content', '')) >= settings['min_feed_chars']:
        return None
    # The server loads the owning feed/user and runs the same scraper, URL/content
    # rewrite rules, HTTP settings and sanitizer as its Fetch original content action.
    # Passing false explicitly is essential: true would mutate stored publisher fields.
    try:
        preview = client.fetch_entry_content(entry['id'], timeout=settings['timeout'],
                                             retries=settings['retries'], max_bytes=settings['max_bytes'])
    except MetadataAPIError as exc:
        if exc.operation != 'fetch_content' or exc.category not in {'transport', 'temporary', 'response_too_large'}:
            raise
        return limited_source(entry, settings, exc.category)
    content = preview['content']
    if feed_text_length(content) < settings['min_article_chars']:
        # Miniflux can return original content if its scraper extracted nothing.
        # Do not treat a wrapper-only preview as successful article retrieval.
        return limited_source(entry, settings, 'insufficient_article_text', content)
    text = markdownify(content).strip()
    limit = settings['max_text_chars']
    logger.info('operation=fetch_article entry_id=%s http_status=200 source=miniflux_article text_chars=%d truncated=%s',
                entry['id'], min(len(text), limit), len(text) > limit)
    return {'kind': 'miniflux_article', 'url': entry.get('url', ''),
            'content': text[:limit], 'truncated': len(text) > limit}
