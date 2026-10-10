import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai import OpenAI, _base_client

from core.get_ai_result import get_ai_result, RequestBudget
from core.process_entries import process_entry
from services.editorial import ARTICLE_PROMPT
from services.llm_diagnostics import LLMError, chat_text, responses_text, provider_error
from services.state import State


# OpenAI 3 uses httpx2; older supported SDK installations use httpx.
httpx = getattr(_base_client, 'httpx2', None) or _base_client.httpx


def completion(content='{"summary":"A summary","rating":"8.5","topics":["Tools"]}', **changes):
    return {'id': 'chatcmpl-fixture', 'object': 'chat.completion', 'created': 1, 'model': 'fixture',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 30, 'total_tokens': 42}} | changes


def native_response(text='{"summary":"A summary","rating":"8.5","topics":["Tools"]}', **changes):
    return {'id': 'resp-fixture', 'object': 'response', 'created_at': 1, 'model': 'fixture', 'status': 'completed',
            'output': [{'id': 'msg-fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
            'usage': {'input_tokens': 12, 'output_tokens': 30, 'total_tokens': 42}} | changes


class LLMContractTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(llm_provider='openai', llm_api='chat_completions',
                                      llm_model='fixture', llm_extra_params={'reasoning_effort': 'low'},
                                      llm_timeout=5, llm_retries=2, llm_max_length=None, language='German')
        self.requests = []

    def client(self, body, status=200):
        def handle(request):
            self.requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(status, json=body)
        client = OpenAI(api_key='SECRET_TEST_CREDENTIAL', base_url='http://gateway.invalid/v1', max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(client.close)
        return client

    def generate(self, client, *, structured=True):
        with patch('core.get_ai_result.runtime', return_value=(self.config, client, RequestBudget(1, 1000))):
            return get_ai_result('Return JSON in ${language}', '{"content":"PRIVATE ARTICLE"}', structured=structured)

    def test_chat_request_contract_remains_explicit_json(self):
        result = self.generate(self.client(completion()))
        path, payload = self.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertIn('German', payload['messages'][0]['content'])
        self.assertEqual(json.loads(result)['rating'], '8.5')

    def test_native_responses_preserves_json_and_maps_reasoning(self):
        self.config.llm_api = 'responses'
        result = self.generate(self.client(native_response()))
        path, payload = self.requests[0]
        self.assertEqual(path, '/v1/responses')
        self.assertEqual(payload['text'], {'format': {'type': 'json_object'}})
        self.assertEqual(payload['reasoning'], {'effort': 'low'})
        self.assertNotIn('reasoning_effort', payload)
        self.assertNotIn('response_format', payload)
        self.assertIn('German', payload['input'][0]['content'])
        self.assertEqual(json.loads(payload['input'][1]['content']), {'content': 'PRIVATE ARTICLE'})
        self.assertEqual(json.loads(result)['rating'], '8.5')

    def test_explicit_prompt_only_mode_omits_api_format_on_both_endpoints(self):
        self.config.llm_structured_output = 'prompt'
        for api in ('chat_completions', 'responses'):
            self.config.llm_api = api
            self.requests.clear()
            body = native_response() if api == 'responses' else completion()
            result = self.generate(self.client(body))
            payload = self.requests[0][1]
            self.assertNotIn('response_format', payload)
            self.assertNotIn('format', payload.get('text', {}))
            messages = payload['input'] if api == 'responses' else payload['messages']
            self.assertIn('Return JSON', messages[0]['content'])
            self.assertEqual(json.loads(result)['rating'], '8.5')

    def test_prompt_only_conflicting_extra_format_is_rejected_before_request(self):
        self.config.llm_structured_output = 'prompt'
        for extra in ({'response_format': {'type': 'json_object'}},
                      {'text': {'format': {'type': 'json_object'}}},
                      {'response_mime_type': 'application/json'}):
            self.config.llm_extra_params = extra
            with self.subTest(extra=extra), self.assertRaises(LLMError) as caught:
                self.generate(self.client(completion()))
            self.assertEqual(caught.exception.category, 'configuration')
            self.assertEqual(self.requests, [])

    def test_native_response_can_generate_unstructured_summary(self):
        self.config.llm_api = 'responses'
        self.assertEqual(self.generate(self.client(native_response(text='A summary')), structured=False), 'A summary')
        self.assertNotIn('text', self.requests[0][1])

    def test_bad_request_exposes_known_parameter_without_body_or_headers(self):
        body = {'error': {'message': "Unsupported parameter: 'response_format'. PRIVATE ARTICLE SECRET_TEST_CREDENTIAL",
                          'type': 'invalid_request_error', 'code': None, 'param': None}}
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(body, 400))
        reason = str(caught.exception)
        self.assertIn('http_status=400', reason)
        self.assertIn('provider_code=unsupported_parameter', reason)
        self.assertIn('parameter=response_format', reason)
        self.assertIn('api=chat_completions', reason)
        self.assertNotIn('PRIVATE', reason)
        self.assertNotIn('SECRET', reason)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(self.requests), 1)

    def test_arbitrary_error_fields_are_not_echoed(self):
        body = {'error': {'message': 'PRIVATE ARTICLE SECRET_TEST_CREDENTIAL', 'param': 'SECRET_PARAM',
                          'code': 'SECRET_CODE', 'type': 'SECRET_TYPE'}}
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(body, 400))
        self.assertIn('provider_code=unknown parameter=None', str(caught.exception))
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_empty_chat_reports_finish_reason_and_safe_counts(self):
        body = completion(content='')
        body['usage']['completion_tokens_details'] = {'reasoning_tokens': 30}
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(body))
        reason = str(caught.exception)
        self.assertIn('category=empty_text http_status=200', reason)
        self.assertIn('finish_reason=stop', reason)
        self.assertIn('completion_tokens=30 reasoning_tokens=30', reason)
        self.assertIn('llm.api: responses', reason)
        self.assertEqual(len(self.requests), 1)  # No regeneration/fallback

    def test_native_failed_response_exposes_controlled_backend_error(self):
        self.config.llm_api = 'responses'
        body = native_response(status='failed', output=[], error={
            'code': 'invalid_request_error', 'message': "Unsupported value: 'text.format.type' PRIVATE ARTICLE"})
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(body))
        reason = str(caught.exception)
        self.assertIn('category=backend_failed http_status=200', reason)
        self.assertIn('response_status=failed output_items=0', reason)
        self.assertIn('provider_code=unsupported_value parameter=text.format.type', reason)
        self.assertNotIn('PRIVATE', reason)
        self.assertEqual(len(self.requests), 1)

    def test_partial_tool_and_refusal_responses_are_not_accepted(self):
        for finish in ('length', 'tool_calls', 'content_filter'):
            body = completion(content='Valid-looking partial text')
            body['choices'][0]['finish_reason'] = finish
            with self.subTest(finish=finish), self.assertRaises(LLMError):
                self.generate(self.client(body))
        body = completion()
        body['choices'][0]['message']['refusal'] = 'PRIVATE REFUSAL'
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(body))
        self.assertIn('refusal_present=True', str(caught.exception))
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_malformed_choices_are_a_safe_protocol_error(self):
        for choices in ([], None):
            with self.subTest(choices=choices), self.assertRaises(LLMError) as caught:
                self.generate(self.client(completion(choices=choices)))
            self.assertEqual(caught.exception.category, 'invalid_response')

    def test_native_incomplete_with_valid_looking_text_is_rejected(self):
        self.config.llm_api = 'responses'
        body = native_response(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(body))
        self.assertIn('incomplete_reason=max_output_tokens', str(caught.exception))

    def test_http_transient_retries_keep_existing_budget(self):
        client = self.client({'error': {'message': 'PRIVATE', 'code': 'server_error'}}, 503)
        with patch('core.get_ai_result.time.sleep') as sleep, self.assertRaises(LLMError) as caught:
            self.generate(client)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_reasoning_conflict_is_rejected_before_request(self):
        self.config.llm_api = 'responses'
        self.config.llm_extra_params['reasoning'] = {'effort': 'medium'}
        with self.assertRaises(LLMError) as caught:
            self.generate(self.client(native_response()))
        self.assertEqual(caught.exception.category, 'configuration')
        self.assertEqual(self.requests, [])

    def test_backend_failure_is_diagnosable_and_does_not_complete_article(self):
        self.config.llm_api = 'responses'
        with tempfile.TemporaryDirectory() as directory:
            self.config.state_path = Path(directory) / 'digest.sqlite3'
            self.config.enrichment_mode = 'metadata'
            self.config.relevance_criteria = 'Fixture policy'
            self.config.metadata_input_fields = []
            self.config.article_prompt = ARTICLE_PROMPT
            self.config.editorial = {'article_words_min': 80, 'article_words_max': 130}
            self.config.agents = {'summary': {'title': 'AI summary:', 'style_block': True}}
            miniflux = Mock()
            entry = {'id': 123, 'content': 'PRIVATE ARTICLE', 'title': 'PRIVATE TITLE', 'meta': {}, 'feed': {}}
            miniflux.get_entry.return_value = entry
            body = native_response(status='failed', output=[], error={'code': 'model_not_found', 'message': 'SECRET PROVIDER TEXT'})
            sdk = self.client(body)
            with patch('core.get_ai_result.runtime', return_value=(self.config, sdk, RequestBudget(1, 1000))), self.assertLogs('miniflux_ai', 'ERROR') as logs:
                with self.assertRaises(LLMError):
                    process_entry(miniflux, entry, self.config)
            text = '\n'.join(logs.output)
            self.assertIn('stage=llm', text)
            self.assertIn('provider_code=model_not_found', text)
            self.assertNotIn('SECRET', text)
            self.assertNotIn('PRIVATE', text)
            miniflux.patch_metadata.assert_not_called()
            self.assertEqual(State(self.config.state_path).queued(), [])
            self.assertIsNone(State(self.config.state_path).get_enrichment(123))

    def test_prompt_only_invalid_json_does_not_complete_article(self):
        self.config.llm_structured_output = 'prompt'
        with tempfile.TemporaryDirectory() as directory:
            self.config.state_path = Path(directory) / 'digest.sqlite3'
            self.config.enrichment_mode = 'metadata'
            self.config.relevance_criteria = 'Fixture policy'
            self.config.metadata_input_fields = []
            self.config.article_prompt = ARTICLE_PROMPT
            self.config.editorial = {'article_words_min': 80, 'article_words_max': 130}
            self.config.agents = {'summary': {'title': 'AI summary:', 'style_block': True}}
            miniflux = Mock()
            entry = {'id': 123, 'content': 'Fixture article', 'title': 'Fixture title', 'meta': {}, 'feed': {}}
            miniflux.get_entry.return_value = entry
            sdk = self.client(completion(content='Not JSON'))
            from services.editorial import GenerationError
            with patch('core.get_ai_result.runtime', return_value=(self.config, sdk, RequestBudget(1, 1000))):
                with self.assertRaises(GenerationError):
                    process_entry(miniflux, entry, self.config)
            self.assertEqual(len(self.requests), 1)
            self.assertNotIn('response_format', self.requests[0][1])
            miniflux.patch_metadata.assert_not_called()
            self.assertEqual(State(self.config.state_path).queued(), [])
            self.assertIsNone(State(self.config.state_path).get_enrichment(123))
