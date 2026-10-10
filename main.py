import concurrent.futures
import time

import schedule

from common import Config, logger
from core import fetch_unread_entries, generate_daily_news
from core.generate_daily_news import refresh_pending
from myapp import app
from services.feeds_status_service import (
    ensure_miniflux_feed,
    generate_feeds_status,
    resolve_feeds_status_url,
)
from services.miniflux_client import create_client


def resolve_ai_news_url(config):
    return config.ai_news_url.rstrip('/') + '/rss/ai-news' if config.ai_news_url else None


def my_schedule(config, miniflux_client):
    interval = config.miniflux_schedule_interval or (15 if config.miniflux_webhook_secret else 1)
    # Keep large unread backlogs from postponing scheduled editions for hours.
    polling = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    polling_future = None

    def poll():
        nonlocal polling_future
        if polling_future is not None:
            if not polling_future.done():
                return
            try:
                polling_future.result()
            except Exception:
                logger.exception('Unread polling failed; retrying')
        polling_future = polling.submit(fetch_unread_entries, config, miniflux_client)

    def ensure_feeds():
        try:
            if config.ai_news_schedule:
                ensure_miniflux_feed(miniflux_client, resolve_ai_news_url(config), 'ai_news')
            if config.feeds_status_enabled:
                ensure_miniflux_feed(miniflux_client, resolve_feeds_status_url(config.feeds_status_url), 'feeds_status')
        except Exception:
            logger.warning('Miniflux unavailable during feed registration; retrying later')

    schedule.every(interval).minutes.do(poll)
    schedule.every(15).minutes.do(ensure_feeds)
    if config.ai_news_schedule:
        for ai_schedule in config.ai_news_schedule:
            schedule.every().day.at(ai_schedule, config.timezone).do(generate_daily_news, miniflux_client)
            logger.info('Added AI news schedule: %s (%s)', ai_schedule, config.timezone)
        schedule.every(5).minutes.do(refresh_pending, miniflux_client)

    if config.feeds_status_enabled:
        schedule.every().day.at(config.feeds_status_schedule, config.timezone).do(
            generate_feeds_status, miniflux_client, config.feeds_status_url,
            file_path=config.feeds_status_path,
        )
    ensure_feeds()
    if config.ai_news_schedule:
        refresh_pending(miniflux_client)
    poll()
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception:
            logger.exception('Scheduled job failed; preserving state and continuing')
            time.sleep(30)


def my_flask(config):
    logger.info('Starting API on port %s', config.flask_port)
    app.run(host='0.0.0.0', port=config.flask_port)


def main():
    config = Config()
    logger.setLevel(config.log_level)
    client = create_client(config)
    # Serve saved RSS even when Miniflux is temporarily unreachable at startup.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(my_schedule, config, client)]
        if config.ai_news_schedule or config.miniflux_webhook_secret or config.feeds_status_enabled:
            futures.append(executor.submit(my_flask, config))
        for future in concurrent.futures.as_completed(futures):
            future.result()


if __name__ == '__main__':
    main()
