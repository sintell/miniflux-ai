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


if __name__ == "__main__":
    unittest.main()
