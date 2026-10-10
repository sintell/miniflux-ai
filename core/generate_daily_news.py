import hashlib
import json
import time
from datetime import datetime, timezone

from common.config import Config
from common.logger import logger
from core.get_ai_result import get_ai_result
from services.editorial import canonical_url, expand_prompt, render_digest, validate_digest
from services.state import State
from services.weather import edition_context, fetch_weather


def refresh_pending(miniflux_client, config=None, state=None):
    config = config or Config()
    state = state or State(config.state_path)
    with state.lock('refresh') as acquired:
        if not acquired:
            return
        ids = state.pending_refresh()
        if not ids:
            return
        try:
            target = config.ai_news_url.rstrip('/') + '/rss/ai-news'
            feed = next((f for f in miniflux_client.get_feeds() if canonical_url(f.get('feed_url')) == canonical_url(target)), None)
            if feed is None:
                raise ValueError('AI news feed is not subscribed at the configured URL')
            miniflux_client.refresh_feed(feed['id'])
            state.refreshed(ids)
        except Exception as exc:
            # Store a bounded diagnostic, not response bodies or credentials.
            state.refreshed(ids, type(exc).__name__)
            logger.warning('Digest stored; Miniflux refresh pending (%s)', type(exc).__name__)


def generate_daily_news(miniflux_client, config=None, *, now=None, llm=None, weather_fetcher=None):
    config = config or Config()
    state = State(config.state_path)
    llm = llm or get_ai_result
    weather_fetcher = weather_fetcher or fetch_weather
    with state.lock() as acquired:
        if not acquired:
            logger.info('Digest generation already running')
            return None
        records = state.queued(config.editorial['max_records'])
        if not records:
            refresh_pending(miniflux_client, config, state)
            return None
        try:
            instant = now or datetime.now(timezone.utc)
            generation_started = time.monotonic()
            context = edition_context(instant, config.timezone, config.editions)
            try:
                weather = weather_fetcher(config.weather, config.timezone, instant, context['edition'])
            except Exception:
                logger.warning('Weather unavailable; generating news-only opening')
                weather = None
            # Bound whole records, never slice serialized JSON or silently drop metadata.
            selected = []
            for record in records:
                candidate = {'context': context, 'weather': weather, 'records': selected + [record]}
                if len(json.dumps(candidate, ensure_ascii=False)) > config.editorial['max_input_chars']:
                    if not selected:
                        raise ValueError('First queued record exceeds max_input_chars; raise the configured budget')
                    break
                selected.append(record)
            request = json.dumps({'context': context, 'weather': weather, 'records': selected}, ensure_ascii=False)
            raw = llm(expand_prompt(config.digest_prompt, config.editorial, config.language), request, structured=True)
            result = validate_digest(raw, selected, config.editorial)
            if weather and time.monotonic() - generation_started > config.weather['max_age_minutes'] * 60:
                # Discard the weather-bearing opening without blocking publication or another LLM call.
                result['opening'] = result['title']
                weather = None
                logger.warning('Forecast freshness budget expired; omitted weather opening')
            ids = [r['record_id'] for r in selected]
            digest_id = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
            digest = {
                'id': digest_id, 'generated_at': context['local_time'], 'edition': context['edition'],
                'title': f"{result['title']} — {context['local_date']}",
                'html': render_digest(result, selected, config.miniflux_browser_url),
                'result': result, 'record_ids': ids, 'weather': weather,
            }
            state.publish(digest, ids)
            logger.info('Stored %s digest with %s stories', context['edition'], len(result['stories']))
        except Exception as exc:
            logger.error('Digest generation failed; queued articles preserved (%s)', type(exc).__name__)
            return None
    refresh_pending(miniflux_client, config, state)
    return digest
