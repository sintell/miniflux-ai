import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


def load_config_class():
    config_path = Path(__file__).resolve().parents[1] / "common" / "config.py"
    spec = importlib.util.spec_from_file_location("config_mod", config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.Config = load_config_class()
        self.old_cwd = os.getcwd()
        self.tmpdir = tempfile.TemporaryDirectory()
        os.chdir(self.tmpdir.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmpdir.cleanup()

    def write_config(self, content):
        Path("config.yml").write_text(content, encoding="utf8")

    def test_extra_params_defaults_to_empty_mapping(self):
        self.write_config("llm:\n  extra_params:\n")

        config = self.Config()

        self.assertEqual(config.llm_extra_params, {})
        self.assertEqual(config.llm_api, 'chat_completions')
        self.assertEqual(config.llm_structured_output, 'json_object')

    def test_explicit_responses_api_configuration(self):
        self.write_config('llm:\n  provider: openai\n  api: responses\n')
        self.assertEqual(self.Config().llm_api, 'responses')
        for content in ('llm:\n  api: auto\n', 'llm:\n  provider: gemini\n  api: responses\n'):
            self.write_config(content)
            with self.assertRaises(ValueError):
                self.Config()

    def test_explicit_prompt_only_structured_output(self):
        self.write_config('llm:\n  structured_output: prompt\n')
        self.assertEqual(self.Config().llm_structured_output, 'prompt')
        self.write_config('llm:\n  structured_output: auto\n')
        with self.assertRaisesRegex(ValueError, 'structured_output'):
            self.Config()

    def test_article_fetch_defaults_and_explicit_options(self):
        self.write_config('{}')
        self.assertEqual(self.Config().article_fetch['mode'], 'thin')
        self.write_config('article_fetch:\n  mode: always\n  timeout: 5\n  max_text_chars: 1000\n')
        self.assertEqual(self.Config().article_fetch['timeout'], 5)
        self.write_config('article_fetch:\n  mode: off\n')
        self.assertEqual(self.Config().article_fetch['mode'], 'off')

    def test_invalid_article_fetch_settings(self):
        for content in ('article_fetch: all', 'article_fetch:\n  mode: auto',
                        'article_fetch:\n  timeout: 31', 'article_fetch:\n  timeout: .nan',
                        'article_fetch:\n  max_bytes: 99999999', 'article_fetch:\n  retries: 4',
                        'article_fetch:\n  min_feed_chars: -1', 'article_fetch:\n  max_text_chars: 100',
                        'article_fetch:\n  unknown: 1'):
            self.write_config(content)
            with self.assertRaises(ValueError):
                self.Config()

    def test_extra_params_accepts_nested_mapping(self):
        self.write_config(
            "llm:\n"
            "  extra_params:\n"
            "    thinking_config:\n"
            "      thinking_budget: 0\n"
        )

        config = self.Config()

        self.assertEqual(
            config.llm_extra_params,
            {"thinking_config": {"thinking_budget": 0}},
        )

    def test_extra_params_rejects_non_mapping(self):
        self.write_config("llm:\n  extra_params: nope\n")

        with self.assertRaises(ValueError):
            self.Config()

    def test_metadata_mode_requires_personal_policy_and_summary_selection(self):
        self.write_config('miniflux:\n  enrichment_mode: metadata\n')
        with self.assertRaisesRegex(ValueError, 'relevance_criteria is required'):
            self.Config()
        self.write_config('miniflux:\n  enrichment_mode: metadata\nmetadata:\n  relevance_criteria: Test policy\n')
        with self.assertRaisesRegex(ValueError, 'agents.summary'):
            self.Config()
        self.write_config('''
miniflux:
  enrichment_mode: metadata
metadata:
  relevance_criteria: Test policy
  input_fields: [another_app.context, another_app.context]
agents:
  summary: {title: 'AI summary:', style_block: true}
''')
        config = self.Config()
        self.assertEqual(config.enrichment_mode, 'metadata')
        self.assertEqual(config.relevance_criteria, 'Test policy')
        self.assertEqual(config.metadata_input_fields, ['another_app.context'])

    def test_legacy_default_and_invalid_metadata_settings(self):
        self.write_config('{}')
        self.assertEqual(self.Config().enrichment_mode, 'legacy_content')
        for content in ('miniflux:\n  enrichment_mode: auto\n',
                        'metadata:\n  input_fields: ["*"]\n',
                        'metadata:\n  input_fields: [unqualified]\n',
                        'metadata:\n  input_fields: a.field\n',
                        'miniflux:\n  retries: 6\n', 'miniflux:\n  timeout: 0\n'):
            self.write_config(content)
            with self.assertRaises(ValueError):
                self.Config()

    def test_language_editorial_units_and_environment(self):
        self.write_config('''
language: ${TEST_LANGUAGE:-English}
miniflux:
  api_key: ${TEST_MINIFLUX_KEY}
llm:
  api_key: ${TEST_LLM_KEY}
  RPM: ${TEST_RPM:-15}
ai_news:
  editorial:
    at_a_glance_words: ${TEST_WORDS:-90}
    max_stories: 5
  weather:
    units: imperial
    enabled: ${TEST_WEATHER:-false}
  prompts:
    article: "${content} in ${language}, ${article_words_max} words"
''')
        secret = 'key: # quoted "value"\nnew line ${NOT_RECURSIVE}'
        with patch.dict(os.environ, {'TEST_LANGUAGE': 'German', 'TEST_MINIFLUX_KEY': 'miniflux-key',
                                     'TEST_LLM_KEY': secret, 'FLASK_PORT': '8088'}):
            config = self.Config()
        self.assertEqual(config.language, 'German')
        self.assertEqual(config.llm_api_key, secret)
        self.assertEqual(config.miniflux_api_key, 'miniflux-key')
        self.assertEqual(config.llm_RPM, 15)
        self.assertEqual(config.editorial['at_a_glance_words'], 90)
        self.assertEqual(config.editorial['max_stories'], 5)
        self.assertEqual(config.weather['units'], 'imperial')
        self.assertFalse(config.weather['enabled'])
        self.assertEqual(config.flask_port, 8088)
        self.assertIn('${language}', config.article_prompt)
        self.assertIn('${content}', config.article_prompt)

    def test_missing_environment_variable_fails_without_leaking_values(self):
        self.write_config('llm:\n  api_key: ${TEST_MISSING_KEY}\n')
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'Missing environment variable: TEST_MISSING_KEY'):
            self.Config()

    def test_defaults_literal_dollars_and_invalid_port(self):
        self.write_config('language: ${TEST_EMPTY:-English}\nllm:\n  api_key: $${LITERAL}\n')
        with patch.dict(os.environ, {'TEST_EMPTY': ''}):
            config = self.Config()
        self.assertEqual(config.language, 'English')
        self.assertEqual(config.llm_api_key, '${LITERAL}')
        for port in ('0', '65536', 'abc'):
            with patch.dict(os.environ, {'FLASK_PORT': port}), self.assertRaises(ValueError):
                self.Config()

    def test_invalid_language_and_weather_configuration(self):
        for value in ('language: ""', 'language: 123', 'ai_news:\n  weather:\n    units: kelvin',
                      'ai_news:\n  weather:\n    enabled: true',
                      'ai_news:\n  editorial:\n    max_stories: 0'):
            self.write_config(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.Config()

    def test_existing_samples_load_with_container_environment(self):
        root = Path(__file__).resolve().parents[1]
        for name, language in (('English', 'English'), ('Chinese', 'Chinese')):
            self.write_config((root / f'config.sample.{name}.yml').read_text(encoding='utf-8'))
            with patch.dict(os.environ, {'MINIFLUX_API_KEY': 'test-only', 'LLM_API_KEY': 'test-only'}, clear=True):
                config = self.Config()
            self.assertEqual(config.language, language)
            self.assertEqual(config.ai_news_url, 'http://miniflux_ai')
            self.assertEqual(config.flask_port, 80)


if __name__ == "__main__":
    unittest.main()
