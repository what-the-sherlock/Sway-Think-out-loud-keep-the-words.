"""End-to-end pipeline check against the real Groq API.

Runs recorded speech through every stage the dictation path uses -- VAD trim,
FLAC encode, whisper-large-v3-turbo, the skip heuristic, the cleanup model
with the configured prompt and parameters, output normalization, and every
safety guardrail -- then reports per-stage latency and whether each clip's
result matched what it should be.

What this does and does not cover: it exercises the real network path, the
real configuration, and the real pure functions, in the order
:class:`flow.engine.Engine` runs them. It does not exercise the hotkey
listener, the audio callback, the overlay, or the clipboard paste, all of
which need a live desktop session and a person holding a key.

Usage::

    python scripts/e2e_check.py                    # uses tests/fixtures/speech
    python scripts/e2e_check.py --clips DIR        # a directory of .wav files
    python scripts/e2e_check.py --repeats 3        # more samples per clip

Every request counts against the Groq free tier, so the script paces itself
and defaults to a single pass.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:  # allow running without installing the package
    sys.path.insert(0, str(_SRC))

from flow.audio.flac import encode_flac  # noqa: E402
from flow.audio.vad import VadTrimmer  # noqa: E402
from flow.cleanup import (  # noqa: E402
    is_cleanup_output_safe,
    normalize_cleanup_output,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from flow.config import load_config  # noqa: E402
from flow.groq_client import GroqClient, GroqError  # noqa: E402
from flow.profiles import CleanupStyle  # noqa: E402

DEFAULT_CLIPS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "speech"


@dataclass
class StageTimes:
    vad_ms: float = 0.0
    encode_ms: float = 0.0
    transcribe_ms: float = 0.0
    cleanup_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.vad_ms + self.encode_ms + self.transcribe_ms + self.cleanup_ms


@dataclass
class ClipResult:
    name: str
    audio_s: float
    trimmed_s: float
    upload_kb: float
    raw: str
    final: str
    language: str | None
    confident: bool
    skipped: bool
    guardrail: str
    times: StageTimes = field(default_factory=StageTimes)


def load_pcm(path: Path, sample_rate: int) -> bytes:
    """Read a wav file as mono little-endian int16 at ``sample_rate``.

    Linear resampling is deliberate: this only feeds Whisper, which is
    robust to it, and it keeps the script free of a scipy dependency the
    application itself does not need.
    """
    data, source_rate = sf.read(path, dtype="int16", always_2d=True)
    mono = data[:, 0].astype(np.float32)
    if source_rate != sample_rate:
        target_len = int(round(len(mono) * sample_rate / source_rate))
        mono = np.interp(
            np.linspace(0, len(mono) - 1, target_len), np.arange(len(mono)), mono
        )
    return mono.astype("<i2").tobytes()


async def run_clip(client: GroqClient, vad: VadTrimmer, config, path: Path) -> ClipResult:
    sample_rate = config.audio.sample_rate
    pcm = load_pcm(path, sample_rate)
    times = StageTimes()

    t0 = time.perf_counter()
    trimmed = vad.trim(pcm)
    times.vad_ms = (time.perf_counter() - t0) * 1000
    if not trimmed:
        raise RuntimeError(f"{path.name}: the VAD found no speech at all")

    t0 = time.perf_counter()
    flac = encode_flac(trimmed, sample_rate, config.audio.channels)
    times.encode_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    transcription = await client.transcribe(flac, language=config.groq.language)
    times.transcribe_ms = (time.perf_counter() - t0) * 1000

    raw = transcription.text
    cfg = config.cleanup
    skipped = (
        not cfg.enabled
        or (cfg.skip_when_unconfident and not transcription.confident)
        or should_skip_cleanup(
            raw, language=transcription.language, min_words=cfg.skip_min_words
        )
    )

    final, guardrail = raw, ""
    if not skipped:
        t0 = time.perf_counter()
        result = await client.cleanup(
            raw,
            system_prompt=system_prompt_for_style(
                CleanupStyle.CASUAL, language=transcription.language
            ),
            model=cfg.model,
            temperature=cfg.temperature,
            reasoning_effort=cfg.reasoning_effort or None,
            max_output_tokens=cfg.max_output_tokens or None,
        )
        times.cleanup_ms = (time.perf_counter() - t0) * 1000
        candidate = normalize_cleanup_output(result.text, raw)
        if not candidate:
            guardrail = "empty"
        elif not is_cleanup_output_safe(raw, candidate):
            guardrail = unsafe_reason(raw, candidate)
        else:
            final = candidate

    return ClipResult(
        name=path.stem,
        audio_s=len(pcm) / 2 / sample_rate,
        trimmed_s=len(trimmed) / 2 / sample_rate,
        upload_kb=len(flac) / 1024,
        raw=raw,
        final=final,
        language=transcription.language,
        confident=transcription.confident,
        skipped=skipped,
        guardrail=guardrail,
        times=times,
    )


def print_report(results: list[ClipResult]) -> None:
    print()
    for name in sorted({r.name for r in results}):
        group = [r for r in results if r.name == name]
        first = group[0]
        print(f"--- {name}  ({first.audio_s:.1f}s -> {first.trimmed_s:.1f}s, {first.upload_kb:.0f}KB)")
        print(f"    language={first.language} confident={first.confident} "
              f"cleanup={'skipped' if first.skipped else 'ran'}"
              + (f" guardrail={first.guardrail}" if first.guardrail else ""))
        print(f"    raw   : {first.raw}")
        if first.final != first.raw:
            print(f"    final : {first.final}")

    header = f"\n{'clip':<16}{'vad':>6}{'enc':>6}{'asr':>8}{'cleanup':>9}{'TOTAL':>9}"
    print(header)
    print("-" * len(header.strip()))
    for name in sorted({r.name for r in results}):
        group = [r for r in results if r.name == name]
        med = lambda key: statistics.median(getattr(g.times, key) for g in group)  # noqa: E731
        print(f"{name:<16}{med('vad_ms'):>6.0f}{med('encode_ms'):>6.0f}"
              f"{med('transcribe_ms'):>8.0f}{med('cleanup_ms'):>9.0f}"
              f"{statistics.median(g.times.total_ms for g in group):>9.0f}")

    print("-" * len(header.strip()))
    for label, key in (("ASR", "transcribe_ms"), ("cleanup", "cleanup_ms")):
        values = sorted(getattr(r.times, key) for r in results)
        print(f"{label:>16}: p50={statistics.median(values):6.0f}ms  p95={_p95(values):6.0f}ms")
    totals = sorted(r.times.total_ms for r in results)
    print(f"{'END-TO-END':>16}: p50={statistics.median(totals):6.0f}ms  p95={_p95(totals):6.0f}ms  "
          f"n={len(totals)}")

    skipped = sum(1 for r in results if r.skipped)
    tripped = [r for r in results if r.guardrail]
    print(f"\ncleanup skip rate: {skipped / len(results) * 100:.0f}% ({skipped}/{len(results)})")
    if tripped:
        print("guardrails tripped (raw transcript pasted instead):")
        for r in tripped:
            print(f"  {r.name}: {r.guardrail}")


def _p95(sorted_values: list[float]) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * 0.95))]


async def main_async(args: argparse.Namespace) -> int:
    config = load_config()
    clips = sorted(Path(args.clips).glob("*.wav"))
    if not clips:
        print(f"no .wav clips found in {args.clips}", file=sys.stderr)
        return 2

    vad = VadTrimmer(
        sample_rate=config.audio.sample_rate,
        frame_ms=config.audio.vad_frame_ms,
        mode=config.audio.vad_mode,
        padding_ms=config.audio.vad_padding_ms,
    )
    client = GroqClient(
        config.groq_api_key,
        base_url=config.groq.base_url,
        timeout_s=config.groq.timeout_s,
        connect_timeout_s=config.groq.connect_timeout_s,
        max_retries=config.groq.max_retries,
    )

    t0 = time.perf_counter()
    await client.warm_up()
    print(f"cleanup model : {config.cleanup.model} (reasoning_effort="
          f"{config.cleanup.reasoning_effort or 'unset'})")
    print(f"warm-up       : {(time.perf_counter() - t0) * 1000:.0f}ms")

    results: list[ClipResult] = []
    try:
        for _ in range(args.repeats):
            for clip in clips:
                try:
                    results.append(await run_clip(client, vad, config, clip))
                except GroqError as exc:
                    print(f"{clip.stem}: FAILED -- {exc}", file=sys.stderr)
                # Free tier: 30 requests/minute org-wide, and long clips also
                # burn the audio-seconds budget. Pace against the clip length.
                await asyncio.sleep(args.pace)
    finally:
        await client.aclose()

    if not results:
        print("every clip failed", file=sys.stderr)
        return 1
    print_report(results)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clips", default=str(DEFAULT_CLIPS), help="directory of .wav clips")
    parser.add_argument("--repeats", type=int, default=1, help="passes over the clip set")
    parser.add_argument("--pace", type=float, default=2.0, help="seconds to wait between requests")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
