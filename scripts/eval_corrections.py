"""Eval harness for self-correction, per the D4 requirement that prompt and
model changes be measurable rather than vibes.

Two things are being measured, and they fail in different ways, so they are
reported separately:

* **Detection** (offline, free, no network). Does :func:`sway.corrections.scan`
  fire on a real correction and stay quiet on ordinary speech? A false negative
  costs the user a tidy-up. A false positive silently deletes words they meant
  to keep, so the negative cases carry the same weight as the positive ones and
  a regression on either is a failure.
* **Resolution** (``--live``, one Groq call per case). Given that detection
  fired, does the pasted text actually contain the value the speaker settled
  on? This is what the user experiences, and it exercises the whole chain --
  directive, model, and the guardrails that can still reject a correct rewrite.

    python scripts/eval_corrections.py              # detection only, offline
    python scripts/eval_corrections.py --live       # + real cleanup calls
    python scripts/eval_corrections.py --live --mode balanced

Commit the numbers this prints when the prompt or the model changes, so the
next change has something to diff against.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sway.cleanup import (  # noqa: E402
    is_cleanup_output_safe,
    normalize_cleanup_output,
    system_prompt_for_style,
    unsafe_reason,
)
from sway.corrections import DEFAULT_MODE, MODES, scan  # noqa: E402
from sway.profiles import CleanupStyle  # noqa: E402


@dataclass(frozen=True, slots=True)
class Case:
    """One transcript and what must become of it.

    ``expect`` is the substring the final text must contain; ``forbid`` is what
    it must not. Substrings rather than exact strings on purpose -- the model
    may legitimately repunctuate, and pinning the whole sentence would turn
    every harmless comma into a failure and make the harness useless.
    """

    text: str
    detect: bool
    expect: str = ""
    forbid: str = ""


#: Real corrections. Every one of these must be detected, and under --live the
#: value the speaker settled on must survive into the pasted text while the one
#: they withdrew must not.
POSITIVE: tuple[Case, ...] = (
    Case("I have a meeting at 7am, no no, it's at 8am.", True, "8am", "7am"),
    Case("Send this to John - actually, send it to Sarah.", True, "Sarah", "John"),
    Case("I'll be there Tuesday, sorry, Wednesday.", True, "Wednesday", "Tuesday"),
    Case("The budget is five thousand - wait, make that fifty thousand.", True, "fifty thousand"),
    Case("I think we should launch in June... actually July would be better.", True, "launch in July", "June"),
    Case("Create a meeting for Monday at 3pm. No, Tuesday at 3pm.", True, "Tuesday", "Monday"),
    Case("We're using Stripe - sorry, I mean Paddle.", True, "Paddle", "Stripe"),
    Case("Let's meet Friday - sorry, Saturday.", True, "Saturday", "Friday"),
    Case("Call me at 10:30 - make that 11.", True, "11", "10:30"),
    Case("Let's do Paris, no, London.", True, "do London", "Paris"),
    Case("Use the old API - sorry, the new API.", True, "new API"),
    Case("Meeting at 7, no, 8.", True, "8"),
)

#: Ordinary speech using the same marker words. None of these may be detected,
#: and under --live none may lose its meaning. The first is the one the brief
#: names explicitly: it must never become "I want to go".
NEGATIVE: tuple[Case, ...] = (
    Case("I don't want to go, actually.", False, "don't want"),
    Case("No, that's exactly right.", False, "exactly right"),
    Case("We shipped Monday and, actually, Tuesday as well.", False, "Monday"),
    Case("I'm sorry about that, it was a mistake.", False, "sorry"),
    Case("Sorry I'm late, the meeting is at 3.", False, "3"),
    Case("No, no, that's important.", False, "important"),
    Case("Wait for the build to finish before you merge.", False, "build"),
    Case("Rather than that, use the other one.", False, "other one"),
    Case("The build is green and the tests pass.", False, "tests pass"),
)

CASES: tuple[Case, ...] = POSITIVE + NEGATIVE


def run_detection(mode: str) -> tuple[int, list[str]]:
    """The offline half. Returns the failure count and the report lines."""
    lines = ["", "DETECTION  (offline, no network)", "-" * 78]
    failures = 0
    for case in CASES:
        result = scan(case.text, mode=mode)
        ok = result.detected == case.detect
        failures += not ok
        want = "fire" if case.detect else "quiet"
        mark = "  ok  " if ok else " FAIL "
        lines.append(f"{mark} expect {want:<5} {case.text}")
        if result.detected:
            lines.append(f"         {result.describe()}  retracted={result.retracted_text()!r}")
    hits = sum(1 for c in CASES if scan(c.text, mode=mode).detected == c.detect)
    lines.append("-" * 78)
    lines.append(f"detection: {hits}/{len(CASES)} correct  ({failures} failed)")
    return failures, lines


async def _cleanup_with_backoff(client, text, config, *, correction):
    """One cleanup call, retried once if the free tier says to wait.

    GroqClient deliberately never retries a 429 -- in the app, making the user
    wait is worse than pasting the raw transcript. A batch harness is the
    opposite case: it is not on anyone's critical path, and a run that dies
    two-thirds through measures nothing.
    """
    import asyncio

    from sway.groq_client import GroqRateLimitError

    for attempt in range(2):
        try:
            return await client.cleanup(
                text,
                system_prompt=system_prompt_for_style(
                    CleanupStyle.CASUAL, correction=correction
                ),
                model=config.cleanup.model,
                temperature=config.cleanup.temperature,
                reasoning_effort=config.cleanup.reasoning_effort or None,
                max_output_tokens=config.cleanup.max_output_tokens or None,
            )
        except GroqRateLimitError as exc:
            if attempt:
                raise
            wait = (getattr(exc, "retry_after_s", None) or 10.0) + 1.0
            print(f"  rate limited; waiting {wait:.0f}s", flush=True)
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


def run_live(mode: str, *, delay: float) -> tuple[int, list[str]]:
    """The online half: one real cleanup call per case.

    Deliberately routed through the same guardrails the engine uses, because a
    rewrite the guardrail rejects is pasted as the raw transcript -- so a
    "correct" model response that trips a guard is still a failure the user
    would see.
    """
    # Imported here, not at module scope, so the offline path needs neither a
    # key nor an event loop.
    import asyncio

    from sway.config import load_config
    from sway.groq_client import GroqClient

    config = load_config()
    lines = ["", f"RESOLUTION  (live, model={config.cleanup.model})", "-" * 78]
    failures = 0

    async def main() -> None:
        nonlocal failures
        # GroqClient exposes aclose() rather than the async context manager
        # protocol, so it is closed explicitly.
        client = GroqClient(
            config.groq_api_key,
            base_url=config.groq.base_url,
            timeout_s=config.groq.timeout_s,
            connect_timeout_s=config.groq.connect_timeout_s,
            max_retries=config.groq.max_retries,
        )
        try:
            for index, case in enumerate(CASES):
                result = scan(case.text, mode=mode)
                if index:
                    # Paced on purpose. The harness sends a request per case
                    # back to back, which is nothing like real dictation and
                    # walks straight into the free tier's tokens-per-minute
                    # limit. The delay is the harness being a good citizen,
                    # not a property of the feature.
                    await asyncio.sleep(delay)
                response = await _cleanup_with_backoff(
                    client, case.text, config, correction=result.detected
                )
                candidate = normalize_cleanup_output(response.text, case.text)
                retracted = result.retracted_text()
                # Exactly the engine's decision, including the fallback.
                if candidate and is_cleanup_output_safe(
                    case.text,
                    candidate,
                    retracted=retracted,
                    correction_detected=result.detected,
                ):
                    final, guard = candidate, ""
                else:
                    final = case.text
                    guard = unsafe_reason(
                        case.text,
                        candidate,
                        retracted=retracted,
                        correction_detected=result.detected,
                    )

                problems = []
                if case.expect and case.expect.lower() not in final.lower():
                    problems.append(f"missing {case.expect!r}")
                if case.forbid and case.forbid.lower() in final.lower():
                    problems.append(f"kept the retracted {case.forbid!r}")
                if guard:
                    problems.append(f"guardrail={guard} (pasted raw)")
                failures += bool(problems)

                lines.append(f"{'  ok  ' if not problems else ' FAIL '} {case.text}")
                lines.append(f"         -> {final}")
                for problem in problems:
                    lines.append(f"         !! {problem}")
        finally:
            await client.aclose()

    asyncio.run(main())
    lines.append("-" * 78)
    lines.append(f"resolution: {len(CASES) - failures}/{len(CASES)} correct  ({failures} failed)")
    return failures, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live",
        action="store_true",
        help="also send each case to the cleanup model (needs a Groq key; ~21 requests)",
    )
    parser.add_argument("--mode", choices=MODES, default=DEFAULT_MODE)
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="seconds between live requests, to stay inside the free tier (default: 3)",
    )
    args = parser.parse_args(argv)

    failures, lines = run_detection(args.mode)
    if args.live:
        live_failures, live_lines = run_live(args.mode, delay=args.delay)
        failures += live_failures
        lines += live_lines

    print("\n".join(lines))
    print()
    print("PASS" if failures == 0 else f"FAIL - {failures} case(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
