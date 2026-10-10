"""Lazy entry points keep utility imports free of configuration and network clients."""

def fetch_unread_entries(*args, **kwargs):
    from .fetch_unread_entries import fetch_unread_entries as run
    return run(*args, **kwargs)


def process_entry(*args, **kwargs):
    from .process_entries import process_entry as run
    return run(*args, **kwargs)


def generate_daily_news(*args, **kwargs):
    from .generate_daily_news import generate_daily_news as run
    return run(*args, **kwargs)
