"""Record a speech fixture so STT changes can be measured instead of guessed.

The multilingual work is blocked on this: `tests/fixtures/speech/` holds eight
English clips and nothing else, so every claim about Hindi or Marathi accuracy
rests on one corrupted sample from a real session. With a fixture we can run
the same audio through both Whisper models, with and without prompt biasing,
as many times as we like -- without anyone re-speaking the sentence.

    python scripts/record_fixture.py mr/office_tomorrow --seconds 6
    # speak when it says RECORDING, then:
    python scripts/eval_stt_fixtures.py

Writes ``tests/fixtures/speech/<name>.wav`` plus a ``.txt`` holding the
ground truth, which is what turns a recording into a test case. Records at the
same rate and channel count the live pipeline uses, so the bytes Groq sees
here are the bytes it sees in the app.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "speech"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="fixture name, e.g. mr/office_tomorrow")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument(
        "--text", default="", help="what you are about to say, as ground truth"
    )
    args = parser.parse_args()

    import sounddevice as sd
    import soundfile as sf

    from sway.config import load_config

    config = load_config()
    rate = config.audio.sample_rate
    channels = config.audio.channels

    path = (FIXTURES / args.name).with_suffix(".wav")
    path.parent.mkdir(parents=True, exist_ok=True)

    print(f"device: {sd.query_devices(kind='input')['name']}")
    print(f"format: {rate}Hz, {channels}ch  ->  {path}")
    for count in (3, 2, 1):
        print(f"  {count}...", flush=True)
        time.sleep(1)
    print(f"  RECORDING for {args.seconds:.0f}s -- speak now", flush=True)

    frames = sd.rec(int(args.seconds * rate), samplerate=rate, channels=channels, dtype="int16")
    sd.wait()
    sf.write(path, frames, rate)

    peak = int(abs(frames).max())
    print(f"  done. peak amplitude {peak}/32767", end="")
    if peak < 2000:
        print("  -- WARNING: very quiet, check the microphone")
    elif peak > 32000:
        print("  -- WARNING: clipping, move back from the microphone")
    else:
        print("  (healthy)")

    if args.text:
        path.with_suffix(".txt").write_text(args.text, encoding="utf-8")
        print(f"  ground truth saved to {path.with_suffix('.txt').name}")
    else:
        print(
            f"  NOTE: no --text given. Write what you said into "
            f"{path.with_suffix('.txt').name} or the fixture cannot be scored."
        )
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
