"""The canonical language table: one row per language Sway can dictate in.

Before this module, a language existed in five places that did not agree --
an ISO-639-1 hint on :class:`sway.config.GroqConfig`, the lowercase English
name Whisper returns in ``verbose_json``, a shortlist for the bar's globe
popover, a *separate* hardcoded list in the dashboard's JavaScript, and a
filler-word dict in :mod:`sway.cleanup`. Nothing mapped codes to names, so
"hi" and "hindi" were different languages as far as the code was concerned,
and Marathi was absent from every list -- meaning a Marathi speaker had no
way to tell Whisper what they were speaking.

Everything is keyed on the ISO-639-1 code, which is what Whisper's
``language`` parameter takes. :func:`normalize` is the single bridge between
that and the English names Whisper *reports*, and every caller that receives
a language from outside routes through it.

Adding a language is one row here: no code changes in the bar, the dashboard,
the filler heuristic, or the prompt builder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

#: Codepoint ranges for scripts whose words end in combining marks, where
#: ``re``'s ``\b`` cannot be used as a word boundary. See
#: :func:`filler_pattern` for why this matters.
_SCRIPT_RANGES: dict[str, str] = {
    "DEVANAGARI": "ऀ-ॿ",
}


@dataclass(frozen=True, slots=True)
class Language:
    """One dictation language and everything that varies by it."""

    #: ISO-639-1, the value Whisper's ``language`` parameter takes.
    code: str
    #: The lowercase English name Whisper reports in ``verbose_json``.
    whisper_name: str
    #: The language's own name, shown in the bar popover.
    endonym: str
    #: The English name, shown in the dashboard.
    english_name: str
    #: Unicode script tokens as :func:`sway.cleanup._scripts` emits them.
    scripts: frozenset[str]
    #: High-precision disfluency markers. See :mod:`sway.cleanup`.
    fillers: tuple[str, ...] = ()
    #: Whitespace tokens per English token for the same meaning. Hindi and
    #: Marathi are more synthetic than English, so the same meaning is
    #: materially fewer tokens; the cleanup skip threshold is scaled by this
    #: or it never fires for them (see :func:`scaled_min_words`).
    word_ratio: float = 1.0
    #: Appended to the cleanup prompt to pin register. This is the difference
    #: between a textbook translation and how the language is actually typed.
    register_hint: str = ""
    #: Scripts a speaker of this language routinely mixes in without it being
    #: a translation -- English loanwords in Latin, for Indic languages. The
    #: guardrail must not treat these as the model going off-script.
    loanword_scripts: frozenset[str] = frozenset()
    #: Whisper's distilled turbo decoder degrades most on lower-resource
    #: languages. Marathi is decoded through a Hindi prior and comes back
    #: corrupted; the full model is worth its extra ~140ms for these.
    needs_accurate_stt: bool = False
    #: Overrides ``[cleanup].model`` for this language when set.
    cleanup_model: str | None = None
    #: Whether the bar's globe popover offers this language directly.
    in_shortlist: bool = True


#: Shared by Hindi and Marathi: the loanword rule is what actually separates
#: writing that reads as native from writing that reads as translated, and it
#: generalises to every Indian language, so it is stated once.
_INDIC_REGISTER = (
    "Write the way this language is actually spoken in everyday conversation, "
    "not the way it is written in a textbook. Keep the ordinary English words "
    "the speaker used (meeting, call, project, email, deadline, client, "
    "schedule, reschedule, update, file, team) in Latin script exactly as "
    "they said them -- never substitute a formal or Sanskritised equivalent, "
    "and never transliterate them into another script. Prefer everyday "
    "colloquial verb endings over literary ones."
)

CATALOGUE: tuple[Language, ...] = (
    Language(
        code="en",
        whisper_name="english",
        endonym="English",
        english_name="English",
        scripts=frozenset({"LATIN"}),
        fillers=("um+", "uh+", "erm+", "hmm+", "you know", "i mean", "sort of", "kind of"),
    ),
    Language(
        code="hi",
        whisper_name="hindi",
        endonym="हिन्दी",
        english_name="Hindi",
        scripts=frozenset({"DEVANAGARI"}),
        fillers=(
            "मतलब",
            "यानी",
            "वो ना",
            "क्या कहते हैं",
        ),
        word_ratio=0.65,
        register_hint=(
            _INDIC_REGISTER + " Use natural spoken Hindi: \"reschedule करना "
            "है\", not \"पुनर्नियोजित करने की इच्छा है\"; \"meeting\", "
            "not \"बैठक\"; \"client\", not \"ग्राहक\"."
        ),
        loanword_scripts=frozenset({"LATIN"}),
        needs_accurate_stt=True,
    ),
    Language(
        code="mr",
        whisper_name="marathi",
        endonym="मराठी",
        english_name="Marathi",
        scripts=frozenset({"DEVANAGARI"}),
        fillers=(
            "म्हणजे",
            "काय म्हणतात",
            "असं ना",
        ),
        word_ratio=0.65,
        register_hint=(
            _INDIC_REGISTER + " Marathi uses आहे-forms, never the Hindi "
            "copula है. Say what a person would say: \"करायचं आहे\" or "
            "\"करायचंय\", never \"करू इच्छितो\"; \"meeting reschedule "
            "करायची आहे\", never \"मीटिंग पुनर्नियोजित करायची आहे\"; "
            "\"client\", never \"क्लायंट\" or \"ग्राहक\"."
        ),
        loanword_scripts=frozenset({"LATIN"}),
        needs_accurate_stt=True,
    ),
    Language(
        code="es",
        whisper_name="spanish",
        endonym="Español",
        english_name="Spanish",
        scripts=frozenset({"LATIN"}),
        fillers=("eh+", "este\\.\\.\\.", "o sea", "bueno", "pues", "digamos"),
    ),
    Language(
        code="fr",
        whisper_name="french",
        endonym="Français",
        english_name="French",
        scripts=frozenset({"LATIN"}),
        fillers=("euh+", "en fait", "tu vois", "bah", "quoi"),
    ),
    Language(
        code="de",
        whisper_name="german",
        endonym="Deutsch",
        english_name="German",
        scripts=frozenset({"LATIN"}),
        fillers=("äh+", "ähm+", "sozusagen", "halt"),
    ),
    Language(
        code="pt",
        whisper_name="portuguese",
        endonym="Português",
        english_name="Portuguese",
        scripts=frozenset({"LATIN"}),
        fillers=("é+", "então\\.\\.\\.", "tipo assim", "quer dizer"),
    ),
    Language(
        code="it",
        whisper_name="italian",
        endonym="Italiano",
        english_name="Italian",
        scripts=frozenset({"LATIN"}),
        fillers=("ehm+", "cioè", "insomma", "diciamo"),
    ),
    Language(
        code="nl",
        whisper_name="dutch",
        endonym="Nederlands",
        english_name="Dutch",
        scripts=frozenset({"LATIN"}),
        fillers=("uh+", "eh+", "zeg maar", "eigenlijk"),
    ),
    Language(
        code="ja",
        whisper_name="japanese",
        endonym="日本語",
        english_name="Japanese",
        scripts=frozenset({"CJK", "HIRAGANA", "KATAKANA"}),
    ),
    Language(
        code="zh",
        whisper_name="chinese",
        endonym="中文",
        english_name="Chinese",
        scripts=frozenset({"CJK"}),
    ),
    Language(
        code="ru",
        whisper_name="russian",
        endonym="Русский",
        english_name="Russian",
        scripts=frozenset({"CYRILLIC"}),
        fillers=("ну", "типа", "короче"),
        in_shortlist=False,
    ),
)

_BY_CODE: dict[str, Language] = {lang.code: lang for lang in CATALOGUE}
_BY_WHISPER_NAME: dict[str, Language] = {lang.whisper_name: lang for lang in CATALOGUE}


def normalize(value: str | None) -> str | None:
    """The canonical ISO-639-1 code for ``value``, or ``None``.

    Accepts a code ("hi", "HI", "hi-IN"), the English name Whisper reports
    ("hindi", "Hindi"), or anything unrecognised -- which yields ``None``
    rather than raising, because this runs on the dictation path against a
    value that came from an API response or a hand-edited config file.
    """
    if not value:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned in _BY_WHISPER_NAME:
        return _BY_WHISPER_NAME[cleaned].code
    # "hi-IN" / "hi_IN" -- a BCP-47 tag where only the language matters here.
    base = re.split(r"[-_]", cleaned, maxsplit=1)[0]
    if base in _BY_CODE:
        return base
    return None


def get(value: str | None) -> Language | None:
    """The :class:`Language` for a code or Whisper name, or ``None``."""
    code = normalize(value)
    return _BY_CODE.get(code) if code else None


def label(value: str | None) -> str:
    """The display name for a language, for the bar popover.

    Falls back to the value upper-cased, so a language configured outside the
    catalogue renders as ``SV`` rather than as blank or as a wrong name.
    """
    if not value:
        return "Auto-detect"
    language = get(value)
    if language is not None:
        return language.endonym
    return value.strip().upper()


def shortlist() -> tuple[tuple[str | None, str], ...]:
    """``(code, label)`` rows for the bar's globe popover, auto-detect first."""
    return ((None, "Auto-detect"),) + tuple(
        (lang.code, lang.endonym) for lang in CATALOGUE if lang.in_shortlist
    )


def options() -> tuple[dict[str, str], ...]:
    """Rows for the dashboard's language selects, served from
    ``/api/settings`` so the UI has no hardcoded list of its own."""
    return tuple(
        {"code": lang.code, "label": lang.english_name, "endonym": lang.endonym}
        for lang in CATALOGUE
        if lang.in_shortlist
    )


def scaled_min_words(min_words: int, value: str | None) -> int:
    """``min_words`` adjusted for how compact this language is.

    The cleanup skip threshold is calibrated in English words. A Hindi or
    Marathi utterance carrying the same meaning is roughly two thirds as many
    whitespace tokens, so an unscaled threshold skips cleanup for essentially
    every Indic dictation -- which is exactly what was happening. Never
    returns less than 1, so a threshold of 0 ("never skip") is preserved.
    """
    language = get(value)
    if language is None or language.word_ratio == 1.0:
        return min_words
    if min_words <= 0:
        return min_words
    return max(1, round(min_words * language.word_ratio))


@lru_cache(maxsize=None)
def filler_pattern(value: str | None) -> re.Pattern[str] | None:
    """The compiled disfluency pattern for a language, or ``None``.

    ``None`` means "known language, no list" *or* "unknown language" -- both
    cases skip the word-list check rather than matching English words against
    other languages; the caller's structural checks still apply.

    Word boundaries are script-aware because ``\\b`` is wrong for Devanagari:
    vowel signs are category ``Mc``, which is not ``isalnum()``, so ``\\w``
    does not match them and ``\\b`` never fires after a word ending in one.
    ``\\b(...)\\b`` therefore never matched most Hindi and Marathi fillers,
    silently disabling the filler check for them -- and with it the escape
    hatch that forces cleanup to run on a short utterance.
    """
    language = get(value)
    if language is None or not language.fillers:
        return None
    body = "|".join(language.fillers)
    ranges = [_SCRIPT_RANGES[s] for s in sorted(language.scripts) if s in _SCRIPT_RANGES]
    if ranges:
        guard = "".join(ranges)
        return re.compile(rf"(?<![{guard}])({body})(?![{guard}])", re.IGNORECASE)
    return re.compile(rf"\b({body})\b", re.IGNORECASE)


def loanword_scripts(value: str | None) -> frozenset[str]:
    """Scripts this language routinely borrows without it being a translation."""
    language = get(value)
    return language.loanword_scripts if language is not None else frozenset()


def scripts_for(value: str | None) -> frozenset[str]:
    """The scripts a correct output in this language may be written in."""
    language = get(value)
    return language.scripts if language is not None else frozenset()


__all__ = [
    "CATALOGUE",
    "Language",
    "filler_pattern",
    "get",
    "label",
    "loanword_scripts",
    "normalize",
    "options",
    "scaled_min_words",
    "scripts_for",
    "shortlist",
]
