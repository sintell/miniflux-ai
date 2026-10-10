import html

from common.config import Config
from common.logger import logger
from core.entry_filter import filter_entry
from core.get_ai_result import get_ai_result
from services.editorial import expand_prompt
from services.state import State


def process_entry(miniflux_client, entry, config=None):
    config = config or Config()
    state = State(config.state_path)
    with state.entry_lock(entry['id']):
        # Re-fetch to avoid processing a stale polling/webhook copy after another worker updated it.
        entry = miniflux_client.get_entry(entry['id'])
        blocks = []
        for name, agent in config.agents.items():
            if not filter_entry(config, (name, agent), entry):
                continue
            try:
                existing = state.get_record(entry['id']) if name == 'summary' else None
                if existing:
                    response = existing['summary']
                else:
                    prompt = config.article_prompt if name == 'summary' else agent['prompt']
                    response = get_ai_result(expand_prompt(prompt, config.editorial, config.language), entry['content'])
                if name == 'summary' and not existing:
                    feed = entry.get('feed') or {}
                    state.enqueue({
                        'entry_id': entry['id'], 'feed_id': feed.get('id', entry.get('feed_id')),
                        'datetime': entry.get('published_at', entry.get('created_at', '')),
                        'category': (feed.get('category') or {}).get('title', ''),
                        'title': entry.get('title', ''), 'summary': response, 'url': entry.get('url', ''),
                        'feed_title': feed.get('title', ''), 'feed_url': feed.get('feed_url', ''),
                        'site_url': feed.get('site_url', ''),
                    })
                title = html.escape(agent.get('title', 'AI summary:'))
                paragraphs = '</p><p>'.join(html.escape(response).split('\n\n'))
                block = f'<p><strong>{title}</strong> {paragraphs}</p>'
                blocks.append(f'<blockquote>{block}</blockquote>' if agent.get('style_block') else block)
            except Exception:
                logger.exception('Error processing entry %s with agent %s', entry['id'], name)
                # Do not mark a partially processed entry: retry using the saved summary.
                raise
        if blocks:
            miniflux_client.update_entry(entry['id'], content='<!-- miniflux-ai -->' + '\n'.join(blocks) + entry['content'])
