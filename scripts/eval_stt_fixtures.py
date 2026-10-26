"""Score Whisper against recorded fixtures, so STT choices stop being guesses.

Answers the four questions the Marathi failure raised, on the same audio:

* Does the full model beat turbo, and by how much?
* Does naming the language in the request help, or is auto-detect enough?
* Does seeding Whisper's ``prompt`` parameter with same-language text improve
  the spelling? (Whisper conditions on that text, so it is the one real
  accuracy lever available without leaving Groq.)
* How long does each option take?

    python scripts/eval_stt_fixtures.py              # every fixture with a .txt
    python scripts/eval_stt_fixtures.py --only mr

Needs fixtures recorded by scripts/record_fixture.py -- a .wav beside a .txt
holding what was actually said. Prints character error rate, which is the
right metric here: the failures are misspellings inside recognisable words,
which a word-level score would round to "everything is wrong".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sway.config import load_config  # noqa: E402
from sway.groq_client import (  # noqa: E402
    WHISPER_ACCURATE_MODEL,
    WHISPER_MODEL,
    GroqClient,
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "speech"

#: Seed text for Whisper's ``prompt``. Whisper conditions its decoding on this,
#: so well-spelled text in the target language biases it toward correct
#: orthography -- the exact failure seen on Marathi, where output was
#: phonetically close but misspelt.
PRIMERS: dict[str, str] = {
    "mr": (
        "मला उद्या ऑफिसला जायचं आहे. आज संध्याकाळी मीटिंग आहे. "
        "मी घरी जातो आहे. तुम्ही कसे आहात? हे काम उद्या पूर्ण करायचं आहे."
    ),
    "hi": (
        "मुझे कल ऑफिस जाना है. आज शाम को मीटिंग है. "
        "मैं घर जा रहा हूँ. आप कैसे हैं? यह काम कल पूरा करना है."
    ),
}


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate: edit distance over reference length."""
    ref, hyp = reference.strip(), hypothesis.strip()
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
            )
        previous = current
    return previous[-1] / len(ref)


def fixtures(only: str) -> list[tuple[Path, str, str]]:
    """Every .wav with a ground-truth .txt, as (path, language, truth)."""
    found = []
    for wav in sorted(FIXTURES.rglob("*.wav")):
        truth = wav.with_suffix(".txt")
        if not truth.exists():
            continue
        # tests/fixtures/speech/mr/x.wav -> "mr"; a loose file -> "en"
        language = wav.parent.name if wav.parent != FIXTURES else "en"
        if only and only != language:
            continue
        found.append((wav, language, truth.read_text(encoding="utf-8").strip()))
    return found


async def run(only: str) -> int:
    config = load_config()
    cases = fixtures(only)
    if not cases:
        print(
            "no scoreable fixtures found.\n"
            "Record one first:\n"
            '  python scripts/record_fixture.py mr/office --seconds 6 '
            '--text "मला उद्या ऑफिसला जायचं आहे"'
        )
        return 1

    client = GroqClient(config.groq_api_key, base_url=config.groq.base_url, timeout_s=60.0)
    try:
        for wav, language, truth in cases:
            audio = wav.read_bytes()
            print(f"\n{wav.relative_to(FIXTURES)}  [{language}]")
            print(f"  said: {truth}")
            variants = [
                ("turbo, auto-detect", WHISPER_MODEL, None, None),
                ("turbo, language named", WHISPER_MODEL, language, None),
                ("large-v3, auto-detect", WHISPER_ACCURATE_MODEL, None, None),
                ("large-v3, language named", WHISPER_ACCURATE_MODEL, language, None),
            ]
            if language in PRIMERS:
                variants.append(
                    ("large-v3, named + primed", WHISPER_ACCURATE_MODEL, language, PRIMERS[language])
                )
            for label, model, hint, primer in variants:
                started = time.perf_counter()
                try:
                    result = await client.transcribe(
                        audio, filename=wav.name, model=model, language=hint, prompt=primer
                    )
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    print(f"  {label:26} ERROR {exc}")
                    continue
                elapsed = (time.perf_counter() - started) * 1000
                score = cer(truth, result.text)
                print(
                    f"  {label:26} CER {score:5.1%}  {elapsed:5.0f}ms  "
                    f"[{result.language}]  {result.text.strip()}"
                )
                await asyncio.sleep(1.0)  # free-tier courtesy
    finally:
        await client.aclose()
    print("\nLower CER is better. 0% is a perfect transcription.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="language folder, e.g. mr")
    return asyncio.run(run(parser.parse_args().only))


if __name__ == "__main__":
    raise SystemExit(main())
