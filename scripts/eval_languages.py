"""Eval harness for the multilingual layer, per the D4 requirement that
prompt and model changes be measurable rather than vibes.

Three things are measured, and they fail in different ways, so they are
reported separately:

* **Registry** (offline, free). Is the single language catalogue internally
  consistent, and does everything that used to keep its own copy of a
  language list now derive from it? A drift here is how Marathi came to be
  unselectable in the first place.
* **Rules** (offline, free). Do the skip heuristic, the filler patterns, and
  the guardrail do what the behaviour matrix says for every edge case --
  including the ones no model call can fix, like Devanagari word boundaries.
* **Output** (``--live``, one Groq call per case). Given a real transcript,
  does the pasted text come back in the right script, non-empty, with the
  speaker's English loanwords intact and without textbook formalism? This is
  what a Hindi or Marathi user actually experiences.

    python scripts/eval_languages.py                    # offline only
    python scripts/eval_languages.py --live             # + real cleanup calls
    python scripts/eval_languages.py --live --model qwen/qwen3.8-27b
    python scripts/eval_languages.py --live --compare   # every candidate model

"Native register" is scored mechanically rather than by a judge model:
``keep`` is the English the speaker used and must survive in Latin script,
and ``forbid`` is the Sanskritised construction that marks a translation as
machine-made. Those two lists *are* the operational definition -- the user
complaint that started this work was precisely a loanword ("reschedule")
being replaced by a formal coinage ("पुनर्नियोजित").

Commit the numbers this prints when the prompt or the model changes, so the
next change has something to diff against.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sway import languages  # noqa: E402
from sway.cleanup import (  # noqa: E402
    CLEANUP_SYSTEM_PROMPT,
    NO_LANGUAGE_INTENT,
    OutputExpectation,
    _scripts,
    has_filler_markers,
    language_intent_for,
    normalize_cleanup_output,
    parse_language_header,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from sway.profiles import CleanupStyle  # noqa: E402
from sway.settings import LANGUAGES, language_label  # noqa: E402

#: Candidate cleanup models on Groq's free tier. gpt-oss-20b is the shipped
#: default; the others are here so a routing decision is a measurement.
CANDIDATE_MODELS = (
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
)


# -- offline: the language registry -------------------------------------------


def check_registry() -> list[str]:
    """Every invariant the catalogue has to hold for the rest to be safe."""
    failures: list[str] = []

    for lang in languages.CATALOGUE:
        if languages.normalize(lang.code) != lang.code:
            failures.append(f"{lang.code}: code does not normalize to itself")
        if languages.normalize(lang.whisper_name) != lang.code:
            failures.append(f"{lang.code}: whisper name {lang.whisper_name!r} does not map back")
        if languages.normalize(lang.code.upper()) != lang.code:
            failures.append(f"{lang.code}: upper-case code does not normalize")
        # The guardrail compares declared scripts against what _scripts()
        # actually emits. A typo here silently disables the check.
        actual = _scripts(lang.endonym)
        if actual and not actual <= set(lang.scripts):
            failures.append(
                f"{lang.code}: endonym is {sorted(actual)} but declares {sorted(lang.scripts)}"
            )
        try:
            languages.filler_pattern(lang.code)
        except Exception as exc:  # pragma: no cover - a bad regex in the table
            failures.append(f"{lang.code}: filler pattern failed to compile ({exc})")

    codes = [lang.code for lang in languages.CATALOGUE]
    if len(codes) != len(set(codes)):
        failures.append("duplicate language codes in the catalogue")

    # The two UI lists must derive from the catalogue, not restate it.
    shortlist_codes = [code for code, _ in LANGUAGES if code]
    catalogue_codes = [lang.code for lang in languages.CATALOGUE if lang.in_shortlist]
    if shortlist_codes != catalogue_codes:
        failures.append(f"settings.LANGUAGES {shortlist_codes} != catalogue {catalogue_codes}")
    option_codes = [row["code"] for row in languages.options()]
    if option_codes != catalogue_codes:
        failures.append(f"languages.options() {option_codes} != catalogue {catalogue_codes}")

    # Marathi is the whole point: it was absent everywhere.
    if languages.get("mr") is None:
        failures.append("Marathi is missing from the catalogue")
    if language_label("mr") == "MR":
        failures.append("Marathi has no endonym label")

    return failures


# -- offline: the rules -------------------------------------------------------


def check_rules() -> list[str]:
    """The skip heuristic, the filler patterns, and the guardrail matrix."""
    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # Devanagari word boundaries. Every one of these used to fail, because
    # `\b` cannot follow a combining vowel sign.
    for text in ("मतलब मुझे घर जाना है", "मुझे घर जाना है यानी अभी", "वो ना मुझे घर जाना है"):
        expect(has_filler_markers(text, language="hi"), f"hindi filler missed: {text}")
    expect(
        not has_filler_markers("मुझे घर जाना है", language="hi"),
        "hindi filler false positive on clean text",
    )
    expect(
        has_filler_markers("मला म्हणजे घरी जायचं आहे", language="mr"),
        "marathi filler missed",
    )
    expect(
        not has_filler_markers("मला घरी जायचं आहे", language="mr"),
        "marathi filler false positive on clean text",
    )
    # The historical default must not move.
    expect(has_filler_markers("um yeah that works", language=None), "english fallback broken")
    expect(
        not has_filler_markers("this is a normal sentence", language="klingon"),
        "unknown language should skip the word list",
    )

    # Skip threshold. A 4-token Hindi utterance is not "short and clean" in
    # the way a 4-word English one is -- it is a whole sentence.
    expect(
        not should_skip_cleanup("मुझे कल की मीटिंग रद्द करनी है", language="hi", min_words=8),
        "hindi sentence still skipping cleanup",
    )
    expect(
        should_skip_cleanup("Meet me at noon", language="en", min_words=8),
        "english skip heuristic changed",
    )
    expect(languages.scaled_min_words(0, "hi") == 0, "a zero threshold must stay zero")

    # Guardrail matrix.
    hi = "मुझे कल की मीटिंग reschedule करनी है"
    mr = "मला उद्याची meeting reschedule करायचंय"
    en = "I need to reschedule tomorrow's meeting"
    to_mr = language_intent_for(source="en", target="mr")
    to_hi = language_intent_for(source="en", target="hi")

    cases: tuple[tuple[str, str, str, object], ...] = (
        ("unrequested hi->en is blocked", hi, en, NO_LANGUAGE_INTENT),
        ("requested en->mr is allowed", en, mr, to_mr),
        ("wrong target language caught", en, en, to_hi),
        ("romanized drift caught", en, "Kal meeting reschedule karni hai", to_mr),
        ("stray script caught", en, mr + " привет", to_mr),
    )
    expected = {
        "unrequested hi->en is blocked": "language_changed",
        "requested en->mr is allowed": "",
        "wrong target language caught": "wrong_target_language",
        "romanized drift caught": "wrong_target_language",
        "stray script caught": "unexpected_script",
    }
    for name, original, cleaned, intent in cases:
        got = unsafe_reason(original, cleaned, intent=intent)
        expect(got == expected[name], f"{name}: expected {expected[name]!r}, got {got!r}")

    # ADR-008: a transform's own settings may never relax the language check.
    loose = OutputExpectation(allows_markdown=True, may_shorten=True, may_lengthen=True)
    expect(
        unsafe_reason(hi, en, expectation=loose) == "language_changed",
        "an OutputExpectation relaxed the language guard",
    )
    # Asking for the language already being spoken is not a translation.
    expect(
        not language_intent_for(source="hindi", target="hi").translating,
        "target == spoken should not count as translating",
    )

    # Header parsing degrades to "no information", never to a wrong answer.
    verdict, body = parse_language_header("#lang: mr mixed=en conf=high\nमला घरी जायचं आहे")
    expect(verdict.code == "mr" and verdict.mixed == "en", "header not parsed")
    expect(body == "मला घरी जायचं आहे", "header not stripped from the body")
    verdict, body = parse_language_header("no header here")
    expect(verdict.code is None and body == "no header here", "missing header not tolerated")
    verdict, body = parse_language_header("#lang: zz garbage\nbody")
    expect(verdict.code is None, "unknown language code should not be trusted")

    # The cacheable prefix (D8.3) must survive every combination.
    combos = (
        {},
        {"language": "hindi"},
        {"target_language": "mr"},
        {"target_language": "hi", "register": "R", "emit_language_header": True},
        {"correction": True, "remove_fillers": False, "target_language": "mr"},
    )
    for kwargs in combos:
        prompt = system_prompt_for_style(CleanupStyle.CASUAL, **kwargs)
        expect(
            prompt.startswith(CLEANUP_SYSTEM_PROMPT),
            f"prompt prefix broken for {kwargs}",
        )
    expect(
        system_prompt_for_style(CleanupStyle.CASUAL) == CLEANUP_SYSTEM_PROMPT,
        "the no-argument prompt is no longer byte-identical",
    )

    return failures


# -- live: output quality -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One transcript and what must become of it.

    ``keep`` and ``forbid`` are substrings, not exact strings: the model may
    legitimately repunctuate, and pinning whole sentences would turn every
    harmless comma into a failure and make the harness useless.
    """

    name: str
    text: str
    #: Whisper's detected language for this transcript.
    language: str
    #: The requested output language, or None for "same as spoken".
    target: str | None = None
    #: English the speaker used that must survive, in Latin script.
    keep: tuple[str, ...] = ()
    #: Textbook constructions that mark the output as machine-translated.
    forbid: tuple[str, ...] = ()
    #: Scripts the output must be written in.
    expect_scripts: frozenset[str] = field(default_factory=frozenset)


LIVE_CASES: tuple[Case, ...] = (
    Case(
        name="marathi cleanup, loanwords kept",
        text="मला उद्या सकाळी meeting reschedule करायची आहे कारण client available नाही",
        language="mr",
        keep=("meeting", "reschedule", "client"),
        forbid=("पुनर्नियोजित", "करू इच्छितो", "ग्राहक"),
        expect_scripts=frozenset({"DEVANAGARI"}),
    ),
    Case(
        name="hindi cleanup, loanwords kept",
        text="मुझे कल की meeting reschedule करनी है क्योंकि client available नहीं है",
        language="hi",
        keep=("meeting", "reschedule", "client"),
        forbid=("पुनर्नियोजित", "बैठक", "इच्छुक"),
        expect_scripts=frozenset({"DEVANAGARI"}),
    ),
    Case(
        name="marathi with fillers",
        text="म्हणजे मला उद्या office ला जायचं आहे पण म्हणजे meeting cancel झाली",
        language="mr",
        keep=("office", "meeting"),
        forbid=("पुनर्नियोजित",),
        expect_scripts=frozenset({"DEVANAGARI"}),
    ),
    Case(
        name="english to marathi",
        text="I want to reschedule tomorrow morning meeting because the client is not available",
        language="en",
        target="mr",
        keep=("meeting",),
        forbid=("पुनर्नियोजित", "करू इच्छितो"),
        expect_scripts=frozenset({"DEVANAGARI"}),
    ),
    Case(
        name="english to hindi",
        text="I want to reschedule tomorrow morning meeting because the client is not available",
        language="en",
        target="hi",
        keep=("meeting",),
        forbid=("पुनर्नियोजित",),
        expect_scripts=frozenset({"DEVANAGARI"}),
    ),
    Case(
        name="hinglish in latin stays latin",
        text="mujhe kal office jaana hai but meeting cancel ho gayi so main ghar pe hi rahunga",
        language="hi",
        keep=("meeting",),
        expect_scripts=frozenset({"LATIN"}),
    ),
    Case(
        name="hindi to marathi",
        text="मुझे कल की meeting reschedule करनी है",
        language="hi",
        target="mr",
        keep=("meeting",),
        expect_scripts=frozenset({"DEVANAGARI"}),
    ),
    Case(
        name="english stays english",
        text="so um I wanted to reschedule the meeting tomorrow because the client isn't around",
        language="en",
        keep=("meeting", "client"),
        expect_scripts=frozenset({"LATIN"}),
    ),
)


@dataclass
class Result:
    case: Case
    output: str
    latency_ms: float
    problems: list[str]

    @property
    def passed(self) -> bool:
        return not self.problems


def run_live(cases: tuple[Case, ...], model: str) -> list[Result]:
    import asyncio

    from sway.config import load_config
    from sway.groq_client import GroqClient

    config = load_config()
    results: list[Result] = []

    async def run_all() -> None:
        # GroqClient exposes aclose() rather than the async context manager
        # protocol, so it is closed explicitly -- same as eval_corrections.
        client = GroqClient(
            config.groq_api_key,
            base_url=config.groq.base_url,
            timeout_s=60.0,
            connect_timeout_s=config.groq.connect_timeout_s,
            max_retries=config.groq.max_retries,
        )
        try:
            for index, case in enumerate(cases):
                if index:
                    # Paced on purpose: back-to-back calls are nothing like
                    # real dictation and walk into the free tier's
                    # tokens-per-minute limit.
                    await asyncio.sleep(1.5)
                target = languages.get(case.target)
                spoken = languages.get(case.language)
                register = (target or spoken).register_hint if (target or spoken) else ""
                prompt = system_prompt_for_style(
                    CleanupStyle.CASUAL,
                    language=spoken.whisper_name if spoken else None,
                    register=register,
                    target_language=target.code if target else None,
                    emit_language_header=case.language != "en",
                )
                started = time.perf_counter()
                try:
                    raw = (
                        await client.cleanup(case.text, system_prompt=prompt, model=model)
                    ).text
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    results.append(
                        Result(
                            case, "", (time.perf_counter() - started) * 1000, [f"error: {exc}"]
                        )
                    )
                    continue
                elapsed = (time.perf_counter() - started) * 1000
                _verdict, body = parse_language_header(raw)
                output = normalize_cleanup_output(body, case.text)
                results.append(Result(case, output, elapsed, _grade(case, output)))
        finally:
            await client.aclose()

    asyncio.run(run_all())
    return results


def _dominant_script(text: str) -> str:
    """The script the sentence is *framed* in.

    Counted per word rather than per character. Devanagari words are shorter
    than the English loanwords a Marathi speaker mixes into them, so a
    correct, natural Marathi sentence has more Latin letters than Devanagari
    ones -- character counting calls it English and is simply wrong. The
    grammatical frame is what matters, and one word is one vote.
    """
    import collections

    votes: collections.Counter[str] = collections.Counter()
    for word in text.split():
        scripts = _scripts(word)
        if len(scripts) == 1:
            votes[next(iter(scripts))] += 1
    return votes.most_common(1)[0][0] if votes else ""


def _grade(case: Case, output: str) -> list[str]:
    problems: list[str] = []
    if not output.strip():
        return ["empty output"]
    # The *dominant* script, not merely an overlapping one. Checking overlap
    # let a fully transliterated rewrite pass on the strength of the English
    # loanwords it happened to keep -- which is exactly the failure that
    # matters for a romanized Hinglish dictation.
    dominant = _dominant_script(output)
    if case.expect_scripts and dominant not in case.expect_scripts:
        problems.append(
            f"wrong script: dominant is {dominant}, wanted {sorted(case.expect_scripts)}"
        )
    for term in case.keep:
        if term.lower() not in output.lower():
            problems.append(f"dropped loanword {term!r}")
    for term in case.forbid:
        if term in output:
            problems.append(f"used formalism {term!r}")
    intent = language_intent_for(source=case.language, target=case.target)
    reason = unsafe_reason(case.text, output, intent=intent)
    if reason:
        problems.append(f"guardrail: {reason}")
    return problems


# -- reporting ----------------------------------------------------------------


def report_live(results: list[Result], model: str) -> bool:
    print(f"\n  model: {model}")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"    [{mark}] {r.case.name}  ({r.latency_ms:.0f}ms)")
        print(f"           -> {r.output[:110] or '(empty)'}")
        for problem in r.problems:
            print(f"           !! {problem}")
    passed = sum(1 for r in results if r.passed)
    empty = sum(1 for r in results if not r.output.strip())
    latencies = [r.latency_ms for r in results]
    print(
        f"    {passed}/{len(results)} passed | empty {empty} | "
        f"median {statistics.median(latencies):.0f}ms"
        if latencies
        else "    no results"
    )
    return passed == len(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make real Groq calls")
    parser.add_argument("--model", default=None, help="cleanup model to test")
    parser.add_argument(
        "--compare", action="store_true", help="run --live against every candidate model"
    )
    args = parser.parse_args()

    ok = True

    print("registry")
    registry_failures = check_registry()
    for failure in registry_failures:
        print(f"    FAIL {failure}")
    print(f"    {'PASS' if not registry_failures else str(len(registry_failures)) + ' failures'}")
    ok = ok and not registry_failures

    print("\nrules")
    rule_failures = check_rules()
    for failure in rule_failures:
        print(f"    FAIL {failure}")
    print(f"    {'PASS' if not rule_failures else str(len(rule_failures)) + ' failures'}")
    ok = ok and not rule_failures

    if args.live or args.compare:
        print("\noutput quality")
        models = CANDIDATE_MODELS if args.compare else (args.model or CANDIDATE_MODELS[0],)
        for model in models:
            ok = report_live(run_live(LIVE_CASES, model), model) and ok

    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
