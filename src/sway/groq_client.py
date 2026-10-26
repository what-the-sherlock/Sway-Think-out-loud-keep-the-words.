"""Async client for Groq's OpenAI-compatible transcription endpoint.

CLAUDE.md: one persistent ``httpx.Client`` with ``http2=True``, created at
startup and pre-warmed with a HEAD request; never one per request. This uses
``httpx.AsyncClient`` specifically because network work runs on a single
asyncio loop in a dedicated thread (see :mod:`flow.net_loop`) rather than
blocking whatever thread calls in.

Retry policy: transient network errors and 5xx responses get one retry with
full-jitter backoff (CLAUDE.md). 429s are surfaced immediately with whichever
specific limit was hit, read from the response headers, and are not
auto-retried — the free tier's request budget (30/min) is small enough that
retrying immediately just consumes it faster.

Timing hooks (D0): :meth:`GroqClient.transcribe` and :meth:`GroqClient.cleanup`
take optional zero-argument callbacks fired the moment the first bytes come
back — response headers for transcription, the first content delta for
cleanup. They take plain callables rather than a tracker so this module stays
unaware of :mod:`flow.latency`; the engine passes closures that close a span.
Each is invoked at most once per call and never on a retried attempt that
subsequently fails.

What is deliberately *not* measured here is upload time on its own. httpx
exposes no hook between "request handed over" and "response headers
received", so the wire time to push the FLAC up, the queue wait at Groq, and
the model's own compute are one indivisible number
(:attr:`~flow.latency.models.Stage.STT_FIRST_BYTE`). Splitting them would
need a custom transport, which D0 does not justify; the FLAC byte count is
annotated on the run instead, so payload size can still be correlated
against that span.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from sway.config import Secret
from sway.errors import SwayError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com"
TRANSCRIPTION_PATH = "/openai/v1/audio/transcriptions"
CHAT_COMPLETIONS_PATH = "/openai/v1/chat/completions"
WHISPER_MODEL = "whisper-large-v3-turbo"

#: The full (non-distilled) Whisper. Turbo's distilled decoder degrades most
#: on lower-resource languages: it decodes Marathi through a Hindi prior,
#: returning both the wrong language label and a corrupted transcript (the
#: Marathi copula "आहे" comes back as the Hindi "है"), which no downstream
#: cleanup or translation can repair. Measured at +140ms median over turbo on
#: this pipeline's fixtures -- affordable inside the latency budget, and paid
#: only by the languages that need it (see Language.needs_accurate_stt).
WHISPER_ACCURATE_MODEL = "whisper-large-v3"

#: CLAUDE.md's Phase 2 named llama-3.1-8b-instant, which Groq has since
#: retired: it now 404s ("The model `llama-3.1-8b-instant` does not exist or
#: you do not have access to it"), which took the whole cleanup stage down.
#: gpt-oss-20b is its replacement on the same free tier and measured *faster*
#: than the old default on this pipeline's prompts -- see
#: DEFAULT_REASONING_EFFORT for the part that actually made it fast.
CLEANUP_MODEL = "openai/gpt-oss-20b"

#: gpt-oss models emit chain-of-thought before their answer unless told
#: otherwise, and a cleanup rewrite needs none of it. Measured over this
#: pipeline's own prompts: "low" p50 381-430ms vs 712-945ms at the model's
#: default effort, for byte-identical rewrites. Cleanup is the single largest
#: stage in the post-release budget, so this is the highest-leverage knob in
#: the app. Ignored (harmlessly) by models that don't support the parameter.
DEFAULT_REASONING_EFFORT = "low"

#: A cleanup rewrite is at most about as long as its input; this only exists
#: to bound the worst case where a model ignores the prompt and starts
#: generating an essay, so the stage can't run away with the latency budget.
DEFAULT_MAX_OUTPUT_TOKENS = 2048

#: Header suffixes Groq (and the OpenAI-compatible convention generally) uses
#: for per-limit-kind remaining-quota counters, e.g. x-ratelimit-remaining-requests.
_RATE_LIMIT_HEADER_KINDS = ("requests", "audio-seconds", "tokens")

#: Groq's 429 *body* names the limit that was actually hit ("...on requests
#: per minute (RPM): Limit 30, Used 30..."), and in practice it does so in
#: cases where no x-ratelimit-remaining-* header reads 0 -- the headers alone
#: reported an anonymous "(429)" for every real rate limit observed against
#: the live API. CLAUDE.md requires surfacing which limit was hit, so the body
#: is parsed as well.
_BODY_LIMIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"requests per (minute|day)\b", re.IGNORECASE), "requests"),
    (re.compile(r"\b(RPM|RPD)\b"), "requests"),
    (re.compile(r"audio (seconds|duration) per (minute|hour|day)\b", re.IGNORECASE), "audio-seconds"),
    (re.compile(r"\b(ASH|ASD)\b"), "audio-seconds"),
    (re.compile(r"tokens per (minute|day)\b", re.IGNORECASE), "tokens"),
    (re.compile(r"\b(TPM|TPD)\b"), "tokens"),
)
_BODY_RETRY_AFTER_RE = re.compile(r"try again in\s+([\d.]+)\s*(ms|s)\b", re.IGNORECASE)


class GroqError(SwayError):
    """A Groq API call failed (after retries, where retries apply)."""


class GroqRateLimitError(GroqError):
    """429 from Groq. ``limit_type``/``retry_after_s`` are parsed from headers when present."""

    def __init__(
        self, message: str, *, limit_type: str | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.limit_type = limit_type
        self.retry_after_s = retry_after_s


#: Below this mean token log-probability, Whisper was guessing: the text is
#: unreliable and so is the language label attached to it. Calibrated against
#: the live API on this project's fixtures, where confident speech scores
#: about -0.12 and Whisper's own documented "failed decode" threshold is -1.0;
#: -0.8 sits inside that, so only genuinely doubtful decodes trip it.
LOW_CONFIDENCE_AVG_LOGPROB = -0.8
#: Above this probability that a segment is pure silence/noise, treat the
#: transcript as untrustworthy -- this is the signal that catches Whisper
#: hallucinating boilerplate ("Thank you for watching!") over near-silence.
HIGH_NO_SPEECH_PROB = 0.6


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    request_id: str | None = None
    #: Whisper's detected spoken language, as its full English name in
    #: lowercase (e.g. ``"english"``, ``"spanish"``) -- exactly what the
    #: ``verbose_json`` response format returns in its ``language`` field.
    #: ``None`` if the API didn't report one.
    language: str | None = None
    #: Mean per-token log-probability across the response's segments, as
    #: ``verbose_json`` reports it. ``None`` when the API returned no
    #: segments. Nearer 0 is more confident.
    avg_logprob: float | None = None
    #: Highest per-segment probability that the audio was not speech at all.
    #: ``None`` when the API returned no segments.
    no_speech_prob: float | None = None

    @property
    def confident(self) -> bool:
        """Whether the transcript (and its language label) can be trusted.

        Gates both the paste itself (see Engine._run_dictation /
        _run_command) and the cleanup rewrite: a doubtful transcript is
        withheld entirely rather than pasted, because the likeliest cause of
        low confidence over a short clip is Whisper hallucinating -- often
        echoing back the personal-dictionary prompt bias -- rather than a
        genuine, just-hard-to-parse utterance.
        """
        if self.avg_logprob is not None and self.avg_logprob < LOW_CONFIDENCE_AVG_LOGPROB:
            return False
        return not (self.no_speech_prob is not None and self.no_speech_prob > HIGH_NO_SPEECH_PROB)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    text: str
    request_id: str | None = None


def compute_retry_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 4.0) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    ceiling = min(cap_s, base_s * (2**attempt))
    return random.uniform(0, ceiling)


def parse_rate_limit(headers: httpx.Headers, body: str | None = None) -> GroqRateLimitError:
    """Build a :class:`GroqRateLimitError` describing exactly which limit was hit.

    Reads the ``x-ratelimit-remaining-*`` headers first, then falls back to
    Groq's 429 body text, which names the limit in prose. The body fallback
    is not optional in practice: against the live API, a real 429 arrived
    with a ``retry-after`` but with no remaining-counter at 0, so headers
    alone yielded an anonymous "(429)".
    """
    retry_after = _parse_float(headers.get("retry-after"))
    hit = [
        kind
        for kind in _RATE_LIMIT_HEADER_KINDS
        if _parse_float(headers.get(f"x-ratelimit-remaining-{kind}")) == 0
    ]
    if not hit and body:
        hit = [kind for pattern, kind in _BODY_LIMIT_PATTERNS if pattern.search(body)]
        hit = list(dict.fromkeys(hit))  # de-duplicate, preserving first-match order
    if retry_after is None and body:
        retry_after = _parse_body_retry_after(body)

    limit_type = ",".join(hit) if hit else None
    message = f"Groq rate limit hit ({limit_type})" if limit_type else "Groq rate limit hit (429)"
    if retry_after is not None:
        message += f"; retry after {retry_after:.0f}s"
    return GroqRateLimitError(message, limit_type=limit_type, retry_after_s=retry_after)


def _parse_body_retry_after(body: str) -> float | None:
    match = _BODY_RETRY_AFTER_RE.search(body)
    if match is None:
        return None
    value = _parse_float(match.group(1))
    if value is None:
        return None
    return value / 1000.0 if match.group(2).lower() == "ms" else value


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class GroqClient:
    def __init__(
        self,
        api_key: Secret,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 15.0,
        connect_timeout_s: float = 5.0,
        max_retries: int = 1,
    ) -> None:
        if not api_key:
            raise GroqError("GROQ_API_KEY is not set")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._max_retries = max_retries
        timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            http2=True,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key.reveal()}"},
        )

    async def warm_up(self) -> None:
        """Best-effort TLS/HTTP2 handshake at startup so the first real request is fast."""
        try:
            await self._client.head("/", timeout=5.0)
        except httpx.HTTPError as exc:
            logger.warning("Groq warm-up request failed (continuing): %s", exc)

    async def check_auth(self) -> None:
        """Verify the key with the cheapest authenticated call Groq offers.

        ``GET /openai/v1/models`` costs no tokens and no audio-request quota,
        which matters because "Test connection" is a button a user may press
        repeatedly and the free tier allows only 2,000 audio requests a day.

        Returns nothing on success; raises the same ``GroqError`` /
        ``GroqRateLimitError`` / ``TimeoutError`` family as every other call
        here, so callers need no special-case handling.
        """
        try:
            response = await self._client.get("/openai/v1/models")
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"Groq did not respond in time: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GroqError(f"could not reach Groq: {exc}") from exc

        if response.status_code == 429:
            raise parse_rate_limit(response.headers, response.text)
        if response.status_code in (401, 403):
            raise GroqError("Groq rejected this API key.")
        if response.status_code >= 400:
            raise GroqError(f"Groq returned {response.status_code}.")

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.flac",
        language: str | None = None,
        prompt: str | None = None,
        model: str | None = None,
        on_first_byte: Callable[[], None] | None = None,
    ) -> TranscriptionResult:
        if not audio:
            raise GroqError("cannot transcribe empty audio")

        # verbose_json (rather than plain json) additionally returns the
        # detected spoken language, which flow.cleanup uses to pick a
        # language-appropriate filler-word heuristic; the "text" field Sway
        # actually needs is present in both formats.
        data: dict[str, str] = {
            "model": model or WHISPER_MODEL,
            "response_format": "verbose_json",
        }
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt

        notify_first_byte = _once(on_first_byte)
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                # stream() rather than post() purely so the arrival of the
                # response *headers* is observable; the body is read in full
                # immediately afterwards, so callers see identical behaviour
                # to the plain POST this replaced.
                async with self._client.stream(
                    "POST",
                    TRANSCRIPTION_PATH,
                    data=data,
                    files={"file": (filename, audio, "audio/flac")},
                ) as response:
                    notify_first_byte()
                    await response.aread()
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(compute_retry_delay(attempt))
                    continue
                raise GroqError(f"network error calling Groq: {exc}") from exc

            if response.status_code == 429:
                raise parse_rate_limit(response.headers, response.text)

            if response.status_code >= 500:
                last_error = GroqError(f"Groq server error {response.status_code}")
                if attempt < self._max_retries:
                    await asyncio.sleep(compute_retry_delay(attempt))
                    continue
                raise last_error

            if response.status_code >= 400:
                raise GroqError(_client_error_message(response))

            return _parse_transcription(response)

        raise GroqError(f"Groq request failed: {last_error}")  # pragma: no cover - unreachable

    async def cleanup(
        self,
        transcript: str,
        *,
        system_prompt: str,
        model: str = CLEANUP_MODEL,
        temperature: float = 0.0,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
        max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
        on_first_token: Callable[[], None] | None = None,
    ) -> CleanupResult:
        """Rewrite ``transcript`` with the cleanup model. Uses Groq's streaming
        chat-completions endpoint (CLAUDE.md: "stream the response") but waits
        for the full text before returning, since injection pastes once rather
        than typing token by token.

        ``reasoning_effort`` is sent only when set; see
        :data:`DEFAULT_REASONING_EFFORT` for why the default roughly halves
        this stage's latency.
        """
        if not transcript:
            raise GroqError("cannot clean up an empty transcript")

        payload: dict[str, object] = {
            "model": model,
            "temperature": temperature,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if max_output_tokens:
            payload["max_completion_tokens"] = max_output_tokens

        notify_first_token = _once(on_first_token)
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._client.stream(
                    "POST", CHAT_COMPLETIONS_PATH, json=payload
                ) as response:
                    if response.status_code == 429:
                        await response.aread()
                        raise parse_rate_limit(response.headers, response.text)

                    if response.status_code >= 500:
                        last_error = GroqError(f"Groq server error {response.status_code}")
                    elif response.status_code >= 400:
                        await response.aread()
                        raise GroqError(_client_error_message(response))
                    else:
                        text = await _read_stream_text(response, notify_first_token)
                        return CleanupResult(
                            text=text, request_id=response.headers.get("x-request-id")
                        )
            except httpx.HTTPError as exc:
                last_error = exc

            if attempt < self._max_retries:
                await asyncio.sleep(compute_retry_delay(attempt))
                continue
            raise GroqError(f"Groq cleanup request failed: {last_error}") from last_error

        raise GroqError(f"Groq cleanup request failed: {last_error}")  # pragma: no cover - unreachable

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_transcription(response: httpx.Response) -> TranscriptionResult:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GroqError(f"Groq returned a non-JSON response: {exc}") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise GroqError("Groq response was missing a 'text' field")
    language = payload.get("language") if isinstance(payload, dict) else None
    if not isinstance(language, str) or not language.strip():
        language = None
    else:
        language = language.strip().lower()
    avg_logprob, no_speech_prob = _parse_confidence(payload)
    return TranscriptionResult(
        text=text.strip(),
        request_id=response.headers.get("x-request-id"),
        language=language,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
    )


def _parse_confidence(payload: object) -> tuple[float | None, float | None]:
    """Mean ``avg_logprob`` and worst ``no_speech_prob`` across the segments
    ``verbose_json`` returns, or ``(None, None)`` if the response carried no
    usable segments (older/plain formats, or a genuinely empty transcript).

    The mean is taken unweighted rather than by segment duration: segments
    are near-uniform in length, and a short garbled segment is exactly the
    signal worth keeping rather than diluting.
    """
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return None, None
    logprobs = [
        s["avg_logprob"]
        for s in segments
        if isinstance(s, dict) and isinstance(s.get("avg_logprob"), (int, float))
    ]
    no_speech = [
        s["no_speech_prob"]
        for s in segments
        if isinstance(s, dict) and isinstance(s.get("no_speech_prob"), (int, float))
    ]
    return (
        sum(logprobs) / len(logprobs) if logprobs else None,
        max(no_speech) if no_speech else None,
    )


def _once(callback: Callable[[], None] | None) -> Callable[[], None]:
    """Wrap ``callback`` so it fires at most once and can never raise.

    Two reasons this is not just ``callback()`` at the call site. A retried
    request would otherwise fire it twice, closing an already-closed span and
    recording a duration that measures the *second* attempt while looking
    like the first. And a measurement callback that throws must not be able
    to fail a dictation that was otherwise about to succeed -- CLAUDE.md's
    rule that instrumentation never takes the app down.
    """
    if callback is None:
        return _noop
    fired = False

    def fire() -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        try:
            callback()
        except Exception:  # pragma: no cover - defensive
            logger.exception("latency callback failed; continuing")

    return fire


def _noop() -> None:
    return None


def _extract_delta(line: str) -> str | None:
    """Parse one SSE line from a chat-completions stream into its content delta.

    Returns ``None`` for blank lines, non-``data:`` lines (SSE comments/other
    fields), the terminal ``[DONE]`` marker, and events with no content delta
    (e.g. the closing chunk, which carries only ``finish_reason``). Malformed
    JSON is treated the same as "nothing to add" rather than failing the
    whole stream over one bad chunk.
    """
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = event.get("choices") if isinstance(event, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else None


async def _read_stream_text(
    response: httpx.Response, on_first_token: Callable[[], None] | None = None
) -> str:
    chunks: list[str] = []
    async for line in response.aiter_lines():
        content = _extract_delta(line)
        if content:
            # Fired on the first delta that carries actual text, not on the
            # first SSE line: the stream opens with role/keep-alive events
            # that would report a first-token time earlier than any token
            # really arrived.
            if not chunks and on_first_token is not None:
                on_first_token()
            chunks.append(content)
    return "".join(chunks).strip()


def _client_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        detail = payload.get("error", {}).get("message") if isinstance(payload, dict) else payload
    except ValueError:
        detail = response.text[:300]
    return f"Groq rejected the request ({response.status_code}): {detail}"


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "CLEANUP_MODEL",
    "WHISPER_ACCURATE_MODEL",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_REASONING_EFFORT",
    "HIGH_NO_SPEECH_PROB",
    "LOW_CONFIDENCE_AVG_LOGPROB",
    "CleanupResult",
    "DEFAULT_BASE_URL",
    "GroqClient",
    "GroqError",
    "GroqRateLimitError",
    "TRANSCRIPTION_PATH",
    "TranscriptionResult",
    "WHISPER_MODEL",
    "compute_retry_delay",
    "parse_rate_limit",
]
