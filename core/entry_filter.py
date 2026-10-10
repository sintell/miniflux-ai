import fnmatch

# Built-in deny list for internal feeds that should never be processed by any agent
BUILTIN_DENY_LIST = [
    'https://ai-news.miniflux',
    'https://feeds-status.miniflux',
]

def filter_entry(config, agent, entry):
    if entry['content'].startswith('<!-- miniflux-ai -->'):
        return False
    start_with_list = [name[1]['title'] for name in config.agents.items()]
    # A publisher's leading quote is not evidence of previous metadata enrichment.
    # Keep the old content-injection heuristic only for the legacy path.
    if getattr(config, 'enrichment_mode', 'legacy_content') != 'metadata':
        if any(agent['style_block'] for agent in config.agents.values()):
            start_with_list.append('<blockquote>')

    # Todo Compatible with whitelist/blacklist parameter, to be removed
    allow_list = agent[1].get('allow_list') if agent[1].get('allow_list') is not None else agent[1].get('whitelist')
    deny_list = agent[1]['deny_list'] if agent[1].get('deny_list') is not None else agent[1].get('blacklist')

    # Always block internal URLs regardless of agent config
    site_url = (entry.get('feed') or {}).get('site_url') or ''
    if any(fnmatch.fnmatch(site_url, pattern) for pattern in BUILTIN_DENY_LIST):
        return False

    # filter, if not content starts with start flag
    if not entry['content'].startswith(tuple(start_with_list)):

        # filter, if in allow_list
        if allow_list is not None:
            if any(fnmatch.fnmatch(site_url, pattern) for pattern in allow_list):
                return True

        # filter, if not in deny_list
        elif deny_list is not None:
            if any(fnmatch.fnmatch(site_url, pattern) for pattern in deny_list):
                return False
            else:
                return True

        # filter, if allow_list and deny_list are both None
        elif allow_list is None and deny_list is None:
            return True

    return False
