import html

from common.config import Config
from common.logger import logger
from core.entry_filter import filter_entry
from core.get_ai_result import get_ai_result
from services.editorial import expand_prompt, GenerationError
from services.enrichment import enrichment_prompt, generation_input, validate_enrichment
from services.miniflux_client import MetadataAPIError, OUTPUT_FIELDS
from services.state import State
from services.article_source import linked_source


def process_entry(miniflux_client, entry, config=None):
    config = config or Config()
    state = State(config.state_path)
    with state.entry_lock(entry['id']):
        if getattr(config, 'enrichment_mode', 'legacy_content') == 'metadata':
            return process_metadata_entry(miniflux_client, entry, config, state)
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


def source_record(entry, summary):
    feed = entry.get('feed') or {}
    return {
        'entry_id': entry['id'], 'feed_id': feed.get('id', entry.get('feed_id')),
        'datetime': entry.get('published_at', entry.get('created_at', '')),
        'category': (feed.get('category') or {}).get('title', ''),
        'title': entry.get('title', ''), 'summary': summary, 'url': entry.get('url', ''),
        'feed_title': feed.get('title', ''), 'feed_url': feed.get('feed_url', ''),
        'site_url': feed.get('site_url', ''),
    }


def process_metadata_entry(client, entry, config, state):
    entry_id = entry['id']
    stage = 'readiness'
    try:
        saved = state.get_enrichment(entry_id)
        if saved and saved['status'] in ('complete', 'unavailable'):
            return
        # Existing local records are cutover boundaries, not a backlog to enrich.
        if not saved and state.get_record(entry_id):
            return
        client.ensure_metadata(config.metadata_input_fields)
        if not saved:
            stage = 'get_entry'
            entry = client.get_entry(entry_id, meta=list(dict.fromkeys(OUTPUT_FIELDS + config.metadata_input_fields)))
            if not filter_entry(config, ('summary', config.agents['summary']), entry):
                return
            # Existing assignments may belong to another instance with a separate
            # state volume. Do not regenerate already-enriched articles at cutover.
            if all(field in entry.get('meta', {}) for field in OUTPUT_FIELDS):
                return
            client.verify_metadata_write(entry_id)
            stage = 'fetch_source'
            source = linked_source(client, entry, config)
            request = generation_input(entry, config, source)
            stage = 'llm'
            response = get_ai_result(enrichment_prompt(config), request, structured=True)
            stage = 'validation'
            values = validate_enrichment(response, entry.get('tags') or [])
            stage = 'save_result'
            record = source_record(entry, values['summary'])
            saved = state.save_enrichment(entry_id, values, record)
        stage = 'patch_metadata'
        client.verify_metadata_write(entry_id)
        client.patch_metadata(entry_id, saved['values'])
        stage = 'complete'
        state.complete_enrichment(entry_id)
    except Exception as exc:
        if isinstance(exc, MetadataAPIError):
            state.enrichment_failed(entry_id, exc.category)
        reason = str(exc) if isinstance(exc, (MetadataAPIError, GenerationError)) else 'Exception details withheld'
        logger.error('Article enrichment failed: entry_id=%s stage=%s error=%s reason=%s',
                     entry_id, stage, type(exc).__name__, reason)
        raise
