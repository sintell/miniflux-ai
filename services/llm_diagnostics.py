"""Allowlisted LLM diagnostics: never echo provider bodies, prompts or headers."""
import re

from services.editorial import GenerationError


PARAMETERS = {
    'model', 'messages', 'input', 'instructions', 'response_format', 'response_format.type',
    'text', 'text.format', 'text.format.type', 'temperature', 'top_p', 'reasoning',
    'reasoning_effort', 'reasoning.effort', 'max_tokens', 'max_completion_tokens',
    'max_output_tokens', 'stream', 'store',
}
CODES = {
    'unsupported_parameter', 'unsupported_value', 'invalid_parameter', 'invalid_request_error',
    'context_length_exceeded', 'model_not_found', 'rate_limit_exceeded', 'insufficient_quota',
    'invalid_api_key', 'authentication_error', 'permission_denied', 'server_error',
    'content_filter', 'content_policy_violation',
}


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def count(value):
    return len(value) if isinstance(value, (list, tuple)) else 0


def token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class LLMError(GenerationError):
    def __init__(self, category, *, status=None, details='', advice='Check the gateway/model configuration and matching gateway logs.'):
        self.status_code = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None
        self.category = category
        self.retryable = self.status_code in (429, 500, 502, 503, 504) or category in ('transport', 'timeout')
        super().__init__(f'LLM failure: category={category} http_status={self.status_code} '
                         f'retryable={self.retryable} {details}. {advice}')


def request_error(exc, *, api, structured):
    status = field(exc, 'status_code') or field(exc, 'code')
    status = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None
    name = type(exc).__name__
    category = {400: 'request_rejected', 401: 'authentication', 403: 'access',
                404: 'model_or_endpoint_not_found', 429: 'rate_limit'}.get(status, 'provider_error')
    if isinstance(exc, TimeoutError) or name == 'APITimeoutError':
        category = 'timeout'
    elif name == 'APIConnectionError':
        category = 'transport'
    body = field(exc, 'body')
    return provider_error(body, category=category, status=status, details=f'api={api} structured={structured}')


def provider_error(body, *, category='backend_failed', status=None, details=''):
    if isinstance(body, dict) and isinstance(body.get('error'), dict):
        body = body['error']
    raw_code = field(body, 'code')
    code = raw_code if isinstance(raw_code, str) and raw_code in CODES else 'unknown'
    raw_param = field(body, 'param')
    parameter = raw_param if isinstance(raw_param, str) and raw_param in PARAMETERS else None
    message = field(body, 'message')
    if isinstance(message, str):
        # Gateway v0.2.1 can retain only message, dropping param/code. Infer only
        # controlled categories/parameter names, never print any message fragment.
        lower = message.lower()
        if 'unsupported parameter' in lower or 'unknown parameter' in lower:
            code = 'unsupported_parameter'
        elif 'unsupported value' in lower or 'does not support' in lower or 'not supported' in lower:
            code = 'unsupported_value'
        for candidate in sorted(PARAMETERS, key=len, reverse=True):
            if parameter is None and re.search(r"['\"]" + re.escape(candidate) + r"['\"]", message):
                parameter = candidate
                break
    advice = 'Inspect the matching gateway error; raw provider text is withheld.'
    if code in ('unsupported_parameter', 'unsupported_value'):
        advice = 'Check the named request option for this model/API; change configuration explicitly, without automatic fallback.'
    elif code == 'context_length_exceeded':
        advice = 'Reduce article/digest input budgets or use a model with a larger context window.'
    elif code in ('invalid_api_key', 'authentication_error') or status in (401, 403):
        advice = 'Check local gateway credentials and the gateway backend login/access.'
    elif code in ('model_not_found',) or status == 404:
        advice = 'Verify the configured model and API endpoint with the gateway.'
    elif code == 'insufficient_quota':
        advice = 'Check available backend allowance; do not retry generation blindly.'
    return LLMError(category, status=status, details=f'{details} provider_code={code} parameter={parameter}', advice=advice)


def chat_text(response):
    choices = field(response, 'choices')
    if not isinstance(choices, (list, tuple)) or not choices:
        raise LLMError('invalid_response', status=200, details=f'api=chat_completions choices={count(choices)}',
                       advice='Expected a Chat Completions response with at least one choice.')
    choice = choices[0]
    message = field(choice, 'message')
    text = field(message, 'content')
    raw_finish = field(choice, 'finish_reason')
    finish = raw_finish if isinstance(raw_finish, str) and raw_finish in ('stop', 'length', 'tool_calls', 'function_call', 'content_filter') else 'unknown'
    usage = field(response, 'usage')
    details = (f'api=chat_completions choices={len(choices)} finish_reason={finish} '
               f'refusal_present={bool(field(message, "refusal"))} tool_calls={count(field(message, "tool_calls"))} '
               f'completion_tokens={token_count(field(usage, "completion_tokens"))} '
               f'reasoning_tokens={token_count(field(field(usage, "completion_tokens_details"), "reasoning_tokens"))}')
    if finish != 'stop' and finish != 'unknown':
        raise LLMError('incomplete_or_nontext_response', status=200, details=details,
                       advice='Do not accept partial/tool/refusal output as enrichment; inspect finish reason and generation limits.')
    if field(message, 'refusal'):
        raise LLMError('refusal', status=200, details=details, advice='Provider returned a refusal; it is not article enrichment.')
    if not isinstance(text, str) or not text.strip():
        raise LLMError('empty_text', status=200, details=details,
                       advice='No final message text. Gateway v0.2.1 can hide backend failures in chat responses; use explicit llm.api: responses to preserve failure status.')
    return text


def responses_text(response):
    raw_status = field(response, 'status')
    status = raw_status if isinstance(raw_status, str) and raw_status in ('completed', 'incomplete', 'failed', 'cancelled', 'queued', 'in_progress') else 'unknown'
    output = field(response, 'output')
    items = output if isinstance(output, (list, tuple)) else []
    usage = field(response, 'usage')
    details = (f'api=responses response_status={status} output_items={len(items)} '
               f'reasoning_items={sum(field(item, "type") == "reasoning" for item in items)} '
               f'output_tokens={token_count(field(usage, "output_tokens"))}')
    if status == 'failed':
        raise provider_error(field(response, 'error'), status=200, details=details)
    if status != 'completed':
        reason = field(field(response, 'incomplete_details'), 'reason')
        reason = reason if reason in ('max_output_tokens', 'content_filter') else 'unknown'
        raise LLMError('incomplete_response', status=200, details=f'{details} incomplete_reason={reason}',
                       advice='No completed response; inspect backend status and generation limits. Partial text is not accepted.')
    text = field(response, 'output_text')
    if not isinstance(text, str) or not text.strip():
        raise LLMError('empty_text', status=200, details=details, advice='Completed response contains no final output text; inspect matching gateway backend logs.')
    return text
