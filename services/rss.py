from datetime import datetime
from feedgen.feed import FeedGenerator


def render_feed(digests, base_url):
    fg = FeedGenerator()
    fg.id('https://ai-news.miniflux')
    fg.title('֎Newsᴬᴵ for you')
    fg.description('Personal news briefings')
    # Preserve the internal site identity used by the article-processing deny list.
    fg.link(href='https://ai-news.miniflux', rel='alternate')
    fg.link(href=base_url.rstrip('/') + '/rss/ai-news', rel='self')
    for digest in digests:
        item = fg.add_entry(order='append')
        identity = 'https://ai-news.miniflux/digest/' + digest['id']
        item.id(identity)
        item.link(href=base_url.rstrip('/') + '/rss/ai-news#' + digest['id'])
        item.title(digest['title'])
        item.description(digest['html'])
        item.published(datetime.fromisoformat(digest['generated_at']))
    return fg.rss_str(pretty=True)
