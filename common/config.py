from datetime import time
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from yaml import safe_load

from services.editorial import ARTICLE_PROMPT, DIGEST_PROMPT, canonical_url, safe_url


ENV_PLACEHOLDER = re.compile(r'\$\$|\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}')


def expand_environment(value):
    """Expand conventional uppercase environment names after parsing YAML.

    Lowercase prompt placeholders are intentionally left for prompt rendering.
    Secret values are never re-parsed as YAML or recursively expanded.
    """
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        if match.group(0) == '$$':
            return '$'
        name, default = match.groups()
        result = os.environ.get(name)
        if default is not None and not result:
            return default
        if result is None:
            raise ValueError(f'Missing environment variable: {name}')
        return result

    return ENV_PLACEHOLDER.sub(replace, value)


def typed_value(value, default):
    if not isinstance(value, str):
        return value
    if isinstance(default, bool):
        if value.lower() not in ('true', 'false'):
            raise ValueError('Boolean settings must be true or false')
        return value.lower() == 'true'
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value

class Config:
    def __init__(self):
        with open('config.yml', encoding='utf8') as config_file:
            self.c = expand_environment(safe_load(config_file) or {})
        if not isinstance(self.c, dict):
            raise ValueError('Configuration must be a YAML mapping')
        try:
            self.flask_port = int(os.environ.get('FLASK_PORT', '80'))
        except ValueError:
            raise ValueError('FLASK_PORT must be an integer from 1 to 65535') from None
        if not 1 <= self.flask_port <= 65535:
            raise ValueError('FLASK_PORT must be an integer from 1 to 65535')
        self.log_level = self.c.get('log_level', 'INFO')
        self.language = self.c.get('language', 'English')
        if not isinstance(self.language, str) or not self.language.strip() or len(self.language) > 80:
            raise ValueError('language must be a nonempty language name (at most 80 characters)')
        self.language = self.language.strip()

        self.miniflux_base_url = self.get_config_value('miniflux', 'base_url', None)
        self.miniflux_browser_url = self.get_config_value('miniflux', 'browser_url', '')
        if self.miniflux_browser_url and not safe_url(self.miniflux_browser_url):
            raise ValueError('miniflux.browser_url must be an HTTP(S) URL')
        if self.miniflux_browser_url and (urlsplit(self.miniflux_browser_url).query or urlsplit(self.miniflux_browser_url).fragment):
            raise ValueError('miniflux.browser_url must not include a query or fragment')
        self.miniflux_api_key = self.get_config_value('miniflux', 'api_key', None)
        self.enrichment_mode = self.get_config_value('miniflux', 'enrichment_mode', 'legacy_content')
        if self.enrichment_mode not in ('legacy_content', 'metadata'):
            raise ValueError('miniflux.enrichment_mode must be legacy_content or metadata')
        self.miniflux_timeout = self.get_config_value('miniflux', 'timeout', 30)
        if isinstance(self.miniflux_timeout, bool) or not isinstance(self.miniflux_timeout, (int, float)) or not math.isfinite(self.miniflux_timeout) or self.miniflux_timeout <= 0:
            raise ValueError('miniflux.timeout must be positive and finite')
        self.miniflux_retries = self.get_config_value('miniflux', 'retries', 2)
        if isinstance(self.miniflux_retries, bool) or not isinstance(self.miniflux_retries, int) or not 0 <= self.miniflux_retries <= 5:
            raise ValueError('miniflux.retries must be between 0 and 5')
        self.relevance_criteria = self.get_config_value('metadata', 'relevance_criteria', '')
        self.metadata_input_fields = self.get_config_value('metadata', 'input_fields', [])
        from services.miniflux_client import QUALIFIED_FIELD
        if not isinstance(self.metadata_input_fields, list) or any(
                not isinstance(field, str) or not QUALIFIED_FIELD.fullmatch(field) for field in self.metadata_input_fields):
            raise ValueError('metadata.input_fields must be a list of explicit client.field names; wildcard projection is not supported')
        self.metadata_input_fields = list(dict.fromkeys(self.metadata_input_fields))
        if self.enrichment_mode == 'metadata' and (not isinstance(self.relevance_criteria, str) or not self.relevance_criteria.strip()):
            raise ValueError('metadata.relevance_criteria is required in metadata mode; supply your personal relevance policy')
        self.miniflux_webhook_secret = self.get_config_value('miniflux', 'webhook_secret', None)
        self.miniflux_schedule_interval = self.get_config_value('miniflux', 'schedule_interval', None)
        if self.miniflux_schedule_interval is not None:
            self.miniflux_schedule_interval = int(self.miniflux_schedule_interval)
            if self.miniflux_schedule_interval < 1:
                raise ValueError('miniflux.schedule_interval must be positive')

        self.llm_provider = self.get_config_value('llm', 'provider', 'openai')
        self.llm_api = self.get_config_value('llm', 'api', 'chat_completions')
        if self.llm_api not in ('chat_completions', 'responses'):
            raise ValueError('llm.api must be chat_completions or responses')
        if self.llm_provider != 'openai' and self.llm_api != 'chat_completions':
            raise ValueError('llm.api: responses requires llm.provider: openai')
        self.llm_structured_output = self.get_config_value('llm', 'structured_output', 'json_object')
        if self.llm_structured_output not in ('json_object', 'prompt'):
            raise ValueError('llm.structured_output must be json_object or prompt')
        self.llm_base_url = self.get_config_value('llm', 'base_url', None)
        self.llm_api_key = self.get_config_value('llm', 'api_key', None)
        self.llm_model = self.get_config_value('llm', 'model', None)
        self.llm_max_length = self.get_config_value('llm', 'max_length', None)
        if self.llm_max_length is not None:
            self.llm_max_length = int(self.llm_max_length)
            if self.llm_max_length < 1:
                raise ValueError('llm.max_length must be positive')
        self.llm_timeout = self.get_config_value('llm', 'timeout', 60)
        if isinstance(self.llm_timeout, bool) or not isinstance(self.llm_timeout, (int, float)) or not math.isfinite(self.llm_timeout) or self.llm_timeout <= 0:
            raise ValueError('llm.timeout must be positive and finite')
        self.llm_max_workers = self.get_config_value('llm', 'max_workers', 4)
        self.llm_RPM = self.get_config_value('llm', 'RPM', 1000)
        self.llm_extra_params = self.get_config_value('llm', 'extra_params', {})
        self.llm_retries = self.get_config_value('llm', 'retries', 2)
        if self.llm_extra_params is None:
            self.llm_extra_params = {}
        if not isinstance(self.llm_extra_params, dict):
            raise ValueError('llm.extra_params must be a mapping')

        from services.article_source import DEFAULTS as ARTICLE_FETCH_DEFAULTS
        self.article_fetch = dict(ARTICLE_FETCH_DEFAULTS)
        fetch_options = self.c.get('article_fetch') or {}
        if not isinstance(fetch_options, dict) or set(fetch_options) - set(self.article_fetch):
            raise ValueError('article_fetch must contain only documented retrieval options')
        self.article_fetch.update(fetch_options)
        # PyYAML's YAML 1.1 resolver parses an unquoted `off` as false.
        if self.article_fetch['mode'] is False:
            self.article_fetch['mode'] = 'off'
        if self.article_fetch['mode'] not in ('off', 'thin', 'always'):
            raise ValueError('article_fetch.mode must be off, thin or always')
        bounds = {'min_feed_chars': (1, 10000), 'min_article_chars': (1, 10000),
                  'max_bytes': (1024, 8 * 1024 * 1024), 'max_text_chars': (1, 100000),
                  'retries': (0, 3)}
        for name, (minimum, maximum) in bounds.items():
            value = typed_value(self.article_fetch[name], ARTICLE_FETCH_DEFAULTS[name])
            self.article_fetch[name] = value
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f'article_fetch.{name} must be an integer from {minimum} to {maximum}')
        timeout = typed_value(self.article_fetch['timeout'], 1.0)
        self.article_fetch['timeout'] = timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('article_fetch.timeout must be positive and at most 30 seconds')
        if self.article_fetch['max_text_chars'] < self.article_fetch['min_article_chars']:
            raise ValueError('article_fetch.max_text_chars must cover min_article_chars')

        self.ai_news_url = canonical_url(self.get_config_value('ai_news', 'url', None))
        self.ai_news_schedule = self.get_config_value('ai_news', 'schedule', None)
        self.ai_news_prompts = self.get_config_value('ai_news', 'prompts', None)
        self.ai_news_prompts = self.ai_news_prompts or {}
        self.timezone = self.get_config_value('ai_news', 'timezone', 'UTC')
        ZoneInfo(self.timezone)
        self.editions = self.get_config_value('ai_news', 'editions', {'morning': '05:00', 'evening': '12:00', 'night': '21:00'})
        if set(self.editions) != {'morning', 'evening', 'night'}:
            raise ValueError('ai_news.editions must define morning, evening and night')
        for value in self.editions.values():
            if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}', value):
                raise ValueError('Edition boundaries must use HH:MM local time')
            time.fromisoformat(value)
        if len(set(self.editions.values())) != 3:
            raise ValueError('Edition boundaries must be distinct')
        self.editorial = {
            'at_a_glance_words': 75, 'max_stories': 8, 'max_groups': 3,
            'story_words_min': 40, 'story_words_max': 70,
            'article_words_min': 80, 'article_words_max': 130,
            'max_input_chars': 60000, 'max_records': 100,
        }
        self.editorial.update(self.get_config_value('ai_news', 'editorial', {}) or {})
        for key, value in self.editorial.items():
            value = typed_value(value, 1)
            self.editorial[key] = value
            minimum = 0 if key == 'max_groups' else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f'ai_news.editorial.{key} must be an integer >= {minimum}')
        if self.editorial['max_groups'] > 3:
            raise ValueError('At most three topic groups are supported')
        for prefix in ('story', 'article'):
            if self.editorial[f'{prefix}_words_min'] > self.editorial[f'{prefix}_words_max']:
                raise ValueError(f'Invalid {prefix} word range')
        self.article_prompt = self.ai_news_prompts.get('article', ARTICLE_PROMPT)
        self.digest_prompt = self.ai_news_prompts.get('digest', DIGEST_PROMPT)
        self.weather = {'enabled': False, 'location': '', 'latitude': None, 'longitude': None,
                        'units': 'metric', 'timeout': 5, 'max_age_minutes': 30,
                        'rain_probability_threshold': 30, 'notable_wind_kmh': 30}
        self.weather.update(self.get_config_value('ai_news', 'weather', {}) or {})
        self.weather['enabled'] = typed_value(self.weather['enabled'], False)
        if not isinstance(self.weather['enabled'], bool):
            raise ValueError('weather.enabled must be true or false')
        for key in ('latitude', 'longitude', 'timeout', 'max_age_minutes', 'rain_probability_threshold', 'notable_wind_kmh'):
            if self.weather[key] is not None:
                self.weather[key] = typed_value(self.weather[key], 1.0)
        if self.weather['units'] not in ('metric', 'imperial'):
            raise ValueError('weather.units must be metric or imperial')
        if self.weather['enabled']:
            for key, bound in (('latitude', 90), ('longitude', 180)):
                value = self.weather[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not -bound <= value <= bound:
                    raise ValueError(f'weather.{key} must be valid coordinates')
        for key in ('timeout', 'max_age_minutes', 'notable_wind_kmh'):
            if isinstance(self.weather[key], bool) or not isinstance(self.weather[key], (int, float)) or not math.isfinite(self.weather[key]) or self.weather[key] <= 0:
                raise ValueError(f'weather.{key} must be positive')
        if not 0 <= self.weather['rain_probability_threshold'] <= 100:
            raise ValueError('Invalid rain probability threshold')
        if not 0 < self.weather['timeout'] <= 30:
            raise ValueError('weather.timeout must be at most 30 seconds')
        self.data_dir = Path(self.c.get('state', {}).get('directory', 'data'))
        self.state_path = self.data_dir / 'digest.sqlite3'
        self.feeds_status_path = self.data_dir / 'feeds_status.json'
        for name in ('llm_max_workers', 'llm_RPM'):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ValueError(f'{name} must be positive')
        if not isinstance(self.llm_retries, int) or not 0 <= self.llm_retries <= 5:
            raise ValueError('llm.retries must be between 0 and 5')

        self.feeds_status_enabled = self.get_config_value('feeds_status', 'enabled', False)
        self.feeds_status_url = canonical_url(self.get_config_value('feeds_status', 'url', self.ai_news_url))
        self.feeds_status_schedule = self.get_config_value('feeds_status', 'schedule', '09:00')

        self.agents = self.c.get('agents', {})
        if self.enrichment_mode == 'metadata' and 'summary' not in self.agents:
            raise ValueError('metadata mode requires agents.summary for article selection')

    def get_config_value(self, section, key, default=None):
        return typed_value((self.c.get(section) or {}).get(key, default), default)
