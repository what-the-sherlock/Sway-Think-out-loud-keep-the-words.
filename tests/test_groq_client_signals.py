"""Tests for the two response signals Flow reads back out of Groq: which rate
limit was hit, and how confident Whisper was in what it heard.

Both are parsed from real response shapes captured against the live API --
see the comments on the individual tests for what was actually observed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from sway.groq_client import (
    HIGH_NO_SPEECH_PROB,
    LOW_CONFIDENCE_AVG_LOGPROB,
    TranscriptionResult,
    _parse_transcription,
    parse_rate_limit,
)


def _response(payload: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )


# -- rate limits -------------------------------------------------------------
# Observed live: a genuine 429 from the transcription endpoint arrived with a
# retry-after but with NO x-ratelimit-remaining-* counter at zero, so parsing
# headers alone reported an anonymous "(429)" and told the user nothing about
# which of the free tier's several budgets they had exhausted.


def test_body_names_the_limit_when_headers_do_not():
    body = json.dumps(
        {
            "error": {
                "message": (
                    "Rate limit reached for model `openai/gpt-oss-20b` in organization "
                    "`org_abc` service tier `on_demand` on requests per minute (RPM): "
                    "Limit 30, Used 30, Requested 1. Please try again in 2s."
                ),
                "type": "requests",
                "code": "rate_limit_exceeded",
            }
        }
    )
    error = parse_rate_limit(httpx.Headers({}), body)
    assert error.limit_type == "requests"
    assert error.retry_after_s == pytest.approx(2.0)
    assert "requests" in str(error)


def test_body_retry_after_in_milliseconds_is_converted_to_seconds():
    body = "Rate limit reached ... on tokens per minute (TPM). Please try again in 480ms."
    error = parse_rate_limit(httpx.Headers({}), body)
    assert error.limit_type == "tokens"
    assert error.retry_after_s == pytest.approx(0.48)


def test_body_recognises_the_audio_seconds_budget():
    body = "Rate limit reached ... on audio seconds per day (ASD): Limit 7200, Used 7200."
    assert parse_rate_limit(httpx.Headers({}), body).limit_type == "audio-seconds"


def test_headers_win_over_the_body():
    headers = httpx.Headers({"x-ratelimit-remaining-tokens": "0", "retry-after": "9"})
    body = "Rate limit reached ... on requests per minute (RPM)."
    error = parse_rate_limit(headers, body)
    assert error.limit_type == "tokens"
    assert error.retry_after_s == pytest.approx(9.0)


def test_an_unparseable_body_still_yields_a_usable_error():
    error = parse_rate_limit(httpx.Headers({}), "<html>502 Bad Gateway</html>")
    assert error.limit_type is None
    assert "429" in str(error)


def test_parse_rate_limit_still_works_with_no_body_at_all():
    error = parse_rate_limit(httpx.Headers({"x-ratelimit-remaining-requests": "0"}))
    assert error.limit_type == "requests"


# -- transcription confidence -----------------------------------------------
# verbose_json returns per-segment avg_logprob and no_speech_prob; confident
# speech measured about -0.12 avg_logprob against the live API.


def test_confidence_is_averaged_across_segments():
    result = _parse_transcription(
        _response(
            {
                "text": "hello there",
                "language": "English",
                "segments": [
                    {"avg_logprob": -0.1, "no_speech_prob": 0.01},
                    {"avg_logprob": -0.3, "no_speech_prob": 0.05},
                ],
            }
        )
    )
    assert result.avg_logprob == pytest.approx(-0.2)
    assert result.no_speech_prob == pytest.approx(0.05)  # the worst segment, not the mean
    assert result.language == "english"
    assert result.confident


def test_a_badly_decoded_transcript_is_not_confident():
    result = _parse_transcription(
        _response(
            {
                "text": "mumble mumble",
                "language": "English",
                "segments": [{"avg_logprob": LOW_CONFIDENCE_AVG_LOGPROB - 0.2, "no_speech_prob": 0.0}],
            }
        )
    )
    assert not result.confident


def test_probable_silence_is_not_confident():
    # The signal that catches Whisper hallucinating boilerplate over silence.
    result = _parse_transcription(
        _response(
            {
                "text": "Thank you for watching!",
                "language": "English",
                "segments": [{"avg_logprob": -0.1, "no_speech_prob": HIGH_NO_SPEECH_PROB + 0.2}],
            }
        )
    )
    assert not result.confident


def test_a_response_with_no_segments_is_treated_as_confident():
    # Plain `json` responses carry no segments; absence of evidence must not
    # disable cleanup for every dictation.
    result = _parse_transcription(_response({"text": "hello", "language": "English"}))
    assert result.avg_logprob is None
    assert result.no_speech_prob is None
    assert result.confident


def test_malformed_segments_are_ignored_rather_than_crashing():
    result = _parse_transcription(
        _response(
            {
                "text": "hello",
                "segments": ["not a dict", {"avg_logprob": "nope"}, {"avg_logprob": -0.2}],
            }
        )
    )
    assert result.avg_logprob == pytest.approx(-0.2)


def test_default_transcription_result_is_confident():
    assert TranscriptionResult(text="hi").confident
