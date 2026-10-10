"""One process-wide request budget, including retries and digest calls."""
from collections import deque
from functools import lru_cache
import json
import threading
import time

from markdownify import markdownify as md
from common.config import Config
from services.editorial import GenerationError, LANGUAGE_POLICY
from services.llm_diagnostics import LLMError, chat_text, responses_text, request_error


class RequestBudget:
    def __init__(self, workers, rpm):
        self.slots = threading.BoundedSemaphore(workers)
        self.rpm = rpm
        self.starts = deque()
        self.lock = threading.Lock()

    def __enter__(self):
        self.slots.acquire()
        while True:
            with self.lock:
                now = time.monotonic()
                while self.starts and self.starts[0] <= now - 60:
                    self.starts.popleft()
                if len(self.starts) < self.rpm:
                    self.starts.append(now)
                    return self
                delay = self.starts[0] + 60 - now
            time.sleep(max(delay, 0.01))

    def __exit__(self, *args):
        self.slots.release()


_runtime_lock = threading.Lock()


def runtime():
    # lru_cache alone allows concurrent first calls to create separate budgets.
    with _runtime_lock:
        return _runtime()


@lru_cache(maxsize=1)
def _runtime():
    config = Config()
    if config.llm_provider == 'gemini':
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=config.llm_api_key,
                              http_options=types.HttpOptions(base_url=config.llm_base_url,
                                                             timeout=int(config.llm_timeout * 1000),
                                                             retry_options=types.HttpRetryOptions(attempts=1)))
    else:
        from openai import OpenAI
        client = OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key,
                        max_retries=0, timeout=config.llm_timeout)
    return config, client, RequestBudget(config.llm_max_workers, config.llm_RPM)


def build_messages(prompt, request, config, structured=False):
    if structured:
        # Parse to reject incomplete serialization; never truncate or markdownify records.
        json.loads(request)
        content = request
    else:
        content = md(request)
        if config.llm_max_length:
            content = content[:config.llm_max_length]
    # Keep trusted instructions in the system role, including legacy ${content} prompts.
    language = getattr(config, 'language', 'English')
    instruction = prompt.replace('${content}', '[Source supplied in user message]').replace('${language}', language)
    policy = LANGUAGE_POLICY.replace('${language}', language)
    return [{'role': 'system', 'content': policy + '\n\n' + instruction},
            {'role': 'user', 'content': content}]


def get_ai_result(prompt: str, request: str, *, structured=False):
    config, client, budget = runtime()
    messages = build_messages(prompt, request, config, structured)
    api = getattr(config, 'llm_api', 'chat_completions')
    structured_output = getattr(config, 'llm_structured_output', 'json_object')
    for attempt in range(config.llm_retries + 1):
        try:
            with budget:
                params = dict(config.llm_extra_params)
                if structured and structured_output == 'prompt':
                    # Explicit prompt-only mode must not accidentally send a
                    # format constraint through extra_params. Never auto-fallback.
                    if ('response_format' in params or 'response_mime_type' in params
                            or isinstance(params.get('text'), dict) and 'format' in params['text']):
                        raise LLMError('configuration', details='structured_output=prompt',
                                       advice='Remove response_format, response_mime_type and text.format from extra_params in prompt-only mode.')
                if config.llm_provider == 'gemini':
                    from google.genai import types
                    if structured and structured_output == 'json_object':
                        params['response_mime_type'] = 'application/json'
                    response = client.models.generate_content(
                        model=config.llm_model, contents=messages[1]['content'],
                        config=types.GenerateContentConfig(system_instruction=messages[0]['content'], **params))
                    text = response.text
                else:
                    if api == 'responses':
                        # The NAS gateway accepts native Responses. Its Chat
                        # adapter drops backend failed-status/error information.
                        if 'reasoning_effort' in params:
                            reasoning = params.get('reasoning', {})
                            if not isinstance(reasoning, dict) or 'effort' in reasoning:
                                raise LLMError('configuration', details='parameter=reasoning_effort',
                                               advice='Specify reasoning.effort or reasoning_effort, not both.')
                            params['reasoning'] = dict(reasoning, effort=params.pop('reasoning_effort'))
                        if structured and structured_output == 'json_object':
                            text_config = params.get('text', {})
                            if not isinstance(text_config, dict):
                                raise LLMError('configuration', details='parameter=text', advice='Responses text options must be a mapping.')
                            params['text'] = dict(text_config, format={'type': 'json_object'})
                        response = client.responses.create(
                            model=config.llm_model, input=messages, timeout=config.llm_timeout, **params)
                        text = responses_text(response)
                    else:
                        if structured and structured_output == 'json_object':
                            params['response_format'] = {'type': 'json_object'}
                        response = client.chat.completions.create(
                            model=config.llm_model, messages=messages, timeout=config.llm_timeout, **params)
                        text = chat_text(response)
                if not isinstance(text, str) or not text.strip():
                    raise GenerationError('LLM returned empty text (no message content)')
                return text
        except Exception as exc:
            if isinstance(exc, GenerationError):
                raise
            status = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)
            transient = status in (429, 500, 502, 503, 504) or isinstance(exc, TimeoutError)
            transient = transient or type(exc).__name__ in ('APIConnectionError', 'APITimeoutError')
            if not transient or attempt == config.llm_retries:
                raise request_error(exc, api=api if config.llm_provider == 'openai' else 'gemini', structured=structured) from None
            time.sleep(min(2 ** (attempt + 1), 30))
