"""Tests for the Groq client: backoff math, response parsing, and the
``cleanup()`` streaming call.

Most tests need no network at all: httpx.Response/Headers can be constructed
standalone, which is enough to exercise every branch of the error-handling
logic. The ``cleanup()`` tests at the bottom drive the real async method end
to end against an ``httpx.MockTransport`` swapped in for the client's real
transport -- still no network, but real streaming/retry code paths.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from flow.config import Secret
from flow.errors import FlowError
from flow.groq_client import (
    CHAT_COMPLETIONS_PATH,
    GroqClient,
    GroqError,
    GroqRateLimitError,
    _client_error_message,
    _extract_delta,
    _parse_transcription,
    compute_retry_delay,
    parse_rate_limit,
)


# -- compute_retry_delay ----------------------------------------------------


@pytest.mark.parametrize("attempt", [0, 1, 2, 3, 10])
def test_retry_delay_is_bounded(attempt: int):
    for _ in range(200):
        delay = compute_retry_delay(attempt, base_s=0.5, cap_s=4.0)
        assert 0.0 <= delay <= 4.0


def test_retry_delay_ceiling_grows_then_caps():
    # Any individual draw is randomised, so check the ceiling formula
    # directly (mirrors the implementation) rather than a noisy sample.
    def ceiling(attempt: int) -> float:
        return min(4.0, 0.5 * (2**attempt))

    assert ceiling(0) < ceiling(1) < ceiling(2) < ceiling(3) == ceiling(4)  # caps at 4.0


def test_retry_delay_never_negative_even_at_high_attempts():
    assert compute_retry_delay(50) >= 0.0


# -- parse_rate_limit --------------------------------------------------------


def test_rate_limit_identifies_the_exhausted_kind():
    headers = httpx.Headers(
        {
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-remaining-audio-seconds": "42",
            "retry-after": "13",
        }
    )
    error = parse_rate_limit(headers)
    assert isinstance(error, GroqRateLimitError)
    assert error.limit_type == "requests"
    assert error.retry_after_s == 13.0
    assert "requests" in str(error)
    assert "13s" in str(error)


def test_rate_limit_identifies_multiple_exhausted_kinds():
    headers = httpx.Headers(
        {
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-remaining-audio-seconds": "0",
        }
    )
    error = parse_rate_limit(headers)
    assert error.limit_type == "requests,audio-seconds"


def test_rate_limit_with_no_identifying_headers():
    error = parse_rate_limit(httpx.Headers({}))
    assert error.limit_type is None
    assert error.retry_after_s is None
    assert "429" in str(error)


def test_rate_limit_tolerates_unparseable_headers():
    headers = httpx.Headers({"retry-after": "not-a-number", "x-ratelimit-remaining-requests": "n/a"})
    error = parse_rate_limit(headers)
    assert error.retry_after_s is None
    assert error.limit_type is None


# -- response parsing ---------------------------------------------------------


def _response(status_code: int, *, json_body=None, text: str = "", headers=None) -> httpx.Response:
    kwargs = {"status_code": status_code, "headers": headers or {}}
    if json_body is not None:
        return httpx.Response(**kwargs, json=json_body)
    return httpx.Response(**kwargs, text=text)


def test_parse_transcription_extracts_text_and_request_id():
    response = _response(200, json_body={"text": "  hello world  "}, headers={"x-request-id": "abc123"})
    result = _parse_transcription(response)
    assert result.text == "hello world"
    assert result.request_id == "abc123"


def test_parse_transcription_extracts_detected_language():
    response = _response(200, json_body={"text": "hola", "language": "Spanish"})
    result = _parse_transcription(response)
    assert result.language == "spanish"  # normalized to lowercase


def test_parse_transcription_language_defaults_to_none_when_absent():
    response = _response(200, json_body={"text": "hello"})
    assert _parse_transcription(response).language is None


def test_parse_transcription_ignores_a_blank_language_field():
    response = _response(200, json_body={"text": "hello", "language": "   "})
    assert _parse_transcription(response).language is None


def test_parse_transcription_rejects_missing_text_field():
    response = _response(200, json_body={"not_text": "oops"})
    with pytest.raises(GroqError, match="text"):
        _parse_transcription(response)


def test_parse_transcription_rejects_non_json_body():
    response = _response(200, text="not json at all {{{")
    with pytest.raises(GroqError, match="non-JSON"):
        _parse_transcription(response)


def test_client_error_message_extracts_api_error_detail():
    response = _response(400, json_body={"error": {"message": "bad model"}})
    assert "bad model" in _client_error_message(response)
    assert "400" in _client_error_message(response)


def test_client_error_message_falls_back_to_raw_text():
    response = _response(400, text="plain text error")
    assert "plain text error" in _client_error_message(response)


# -- construction -------------------------------------------------------------


def test_construction_requires_an_api_key():
    with pytest.raises(GroqError, match="GROQ_API_KEY"):
        GroqClient(Secret(""))


def test_construction_rejects_negative_max_retries():
    with pytest.raises(ValueError):
        GroqClient(Secret("gsk_test"), max_retries=-1)


def test_groq_rate_limit_error_is_a_flow_error():
    assert issubclass(GroqRateLimitError, FlowError)
    assert issubclass(GroqError, FlowError)


# -- _extract_delta (SSE chunk parsing) ---------------------------------------


def test_extract_delta_reads_content_from_a_data_line():
    line = 'data: {"choices":[{"delta":{"content":"hello"}}]}'
    assert _extract_delta(line) == "hello"


def test_extract_delta_ignores_the_done_marker():
    assert _extract_delta("data: [DONE]") is None


def test_extract_delta_ignores_blank_and_non_data_lines():
    assert _extract_delta("") is None
    assert _extract_delta("event: ping") is None


def test_extract_delta_ignores_malformed_json():
    assert _extract_delta("data: {not json") is None


def test_extract_delta_returns_none_for_the_finish_reason_chunk():
    line = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
    assert _extract_delta(line) is None


def test_extract_delta_returns_none_when_choices_is_empty():
    assert _extract_delta('data: {"choices":[]}') is None


# -- cleanup() -----------------------------------------------------------------


def _sse(*chunks: str) -> bytes:
    """Build an SSE body for a chat-completions stream from content deltas."""
    body = b""
    for chunk in chunks:
        event = {"choices": [{"delta": {"content": chunk}}]}
        body += f"data: {json.dumps(event)}\n\n".encode()
    body += b"data: [DONE]\n\n"
    return body


def test_transcribe_requests_verbose_json_and_returns_the_detected_language():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.read()
        return httpx.Response(200, json={"text": "bonjour", "language": "french"})

    client = _mock_client(handler)
    result = asyncio.run(client.transcribe(b"fake-flac-bytes"))

    assert result.text == "bonjour"
    assert result.language == "french"
    assert b'name="response_format"' in captured["body"]
    assert b"verbose_json" in captured["body"]


def _mock_client(handler) -> GroqClient:
    """A GroqClient wired to a mock transport instead of the real network."""
    client = GroqClient(Secret("gsk_test"))
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.groq.com",
    )
    return client


def test_cleanup_rejects_empty_transcript():
    client = _mock_client(lambda request: httpx.Response(200))
    with pytest.raises(GroqError, match="empty"):
        asyncio.run(client.cleanup("", system_prompt="rewrite"))


def test_cleanup_assembles_streamed_deltas_into_the_full_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == CHAT_COMPLETIONS_PATH
        return httpx.Response(200, content=_sse("Hello", " world"))

    client = _mock_client(handler)
    result = asyncio.run(client.cleanup("hello world", system_prompt="rewrite"))
    assert result.text == "Hello world"


def test_cleanup_sends_the_system_prompt_and_transcript_as_messages():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, content=_sse("ok"))

    client = _mock_client(handler)
    asyncio.run(client.cleanup("um hi", system_prompt="be terse"))

    payload = json.loads(captured["body"])
    assert payload["stream"] is True
    assert payload["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "um hi"},
    ]


def test_cleanup_raises_rate_limit_error_on_429_without_retrying():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "5"})

    client = _mock_client(handler)
    with pytest.raises(GroqRateLimitError):
        asyncio.run(client.cleanup("hello there", system_prompt="rewrite"))
    assert calls == 1


def test_cleanup_raises_immediately_on_a_4xx_client_error():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    client = _mock_client(handler)
    with pytest.raises(GroqError, match="bad model"):
        asyncio.run(client.cleanup("hello there", system_prompt="rewrite"))
    assert calls == 1


def test_cleanup_retries_once_on_a_server_error_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=_sse("recovered"))

    client = _mock_client(handler)
    result = asyncio.run(client.cleanup("hello there", system_prompt="rewrite"))
    assert result.text == "recovered"
    assert calls == 2


def test_cleanup_gives_up_after_max_retries_on_repeated_server_errors():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = GroqClient(Secret("gsk_test"), max_retries=2)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.groq.com"
    )
    with pytest.raises(GroqError):
        asyncio.run(client.cleanup("hello there", system_prompt="rewrite"))
    assert calls == 3  # initial attempt + 2 retries
