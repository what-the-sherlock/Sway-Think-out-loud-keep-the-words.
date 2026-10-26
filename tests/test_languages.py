"""The language catalogue and everything that now derives from it.

These are the regression tests for how the multilingual layer broke in the
first place: a language list duplicated in four places that quietly drifted
apart, and a word-boundary regex that could not match the script it was
written for.
"""

from __future__ import annotations

import pytest

from sway import languages
from sway.cleanup import _scripts, has_filler_markers, should_skip_cleanup
from sway.settings import LANGUAGES, language_label

# Spelled out so the file stays readable in any editor.
HI_MEETING = "मुझे कल की meeting reschedule करनी है"
MR_MEETING = "मला उद्याची meeting reschedule करायची आहे"
MR_HOME = "मला घरी जायचं आहे"


# -- the catalogue is the single source of truth -------------------------------


def test_marathi_is_selectable():
    """The bug that started this: Marathi existed in no list anywhere, so a
    Marathi speaker had no way to tell Whisper what they were speaking."""
    assert languages.get("mr") is not None
    assert "mr" in [code for code, _ in LANGUAGES]
    assert language_label("mr") == "मराठी"


def test_the_bar_shortlist_derives_from_the_catalogue():
    """settings.LANGUAGES used to be its own hand-maintained copy."""
    assert [code for code, _ in LANGUAGES if code] == [
        lang.code for lang in languages.CATALOGUE if lang.in_shortlist
    ]


def test_the_dashboard_list_derives_from_the_catalogue():
    """app.js used to hold a second list that had already drifted from the
    bar's: different names, a missing language, no shared source."""
    assert [row["code"] for row in languages.options()] == [
        lang.code for lang in languages.CATALOGUE if lang.in_shortlist
    ]


@pytest.mark.parametrize("language", languages.CATALOGUE, ids=lambda lang: lang.code)
def test_every_declared_script_matches_what_the_guardrail_sees(language):
    """The guardrail compares these tokens against cleanup._scripts output.
    A typo here would silently disable the check rather than fail loudly."""
    observed = _scripts(language.endonym)
    if observed:
        assert observed <= set(language.scripts)


def test_codes_are_unique():
    codes = [lang.code for lang in languages.CATALOGUE]
    assert len(codes) == len(set(codes))


# -- normalize is the one bridge between codes and Whisper's names -------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hi", "hi"),
        ("HI", "hi"),
        ("hindi", "hi"),
        ("Hindi", "hi"),
        ("hi-IN", "hi"),
        ("hi_IN", "hi"),
        ("  mr  ", "mr"),
        ("marathi", "mr"),
        ("klingon", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_accepts_codes_and_whisper_names(value, expected):
    assert languages.normalize(value) == expected


def test_label_falls_back_to_the_code_for_an_unlisted_language():
    """A language configured outside the catalogue must render as something,
    not as blank or as the wrong name."""
    assert language_label("sv") == "SV"
    assert language_label(None) == "Auto-detect"


# -- the Devanagari word-boundary bug -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "मतलब मुझे घर जाना है",  # filler ends in a consonant -- always worked
        "मुझे घर जाना है यानी अभी",  # ends in a vowel sign -- never matched
        "वो ना मुझे घर जाना है",  # ends in a vowel sign -- never matched
    ],
)
def test_hindi_fillers_match_regardless_of_final_vowel_sign(text):
    """Devanagari vowel signs are category Mc, which is not isalnum(), so \\w
    does not match them and \\b never fires after a word ending in one.
    \\b(यानी)\\b therefore matched nothing, silently disabling the filler
    check for most Hindi and Marathi words."""
    assert has_filler_markers(text, language="hi")


def test_marathi_fillers_match():
    assert has_filler_markers("मला म्हणजे घरी जायचं आहे", language="mr")


@pytest.mark.parametrize(("text", "code"), [("मुझे घर जाना है", "hi"), (MR_HOME, "mr")])
def test_clean_indic_speech_has_no_filler_markers(text, code):
    assert not has_filler_markers(text, language=code)


def test_an_undetected_language_still_uses_the_english_list():
    """The historical default. Changing it would silently stop catching
    fillers on every transcript Whisper failed to label."""
    assert has_filler_markers("um yeah that works", language=None)


def test_an_unknown_language_skips_the_word_list_but_keeps_structure():
    assert not has_filler_markers("this is a normal sentence", language="klingon")
    assert has_filler_markers("home home is where I want to go", language="klingon")


# -- the skip threshold --------------------------------------------------------


def test_indic_sentences_no_longer_skip_cleanup():
    """Every Indic dictation in the real history DB came out byte-identical to
    its input: an 8-English-word threshold is 12+ Hindi words of meaning, so
    ordinary sentences sat under it and never reached the model."""
    assert not should_skip_cleanup("मुझे कल की मीटिंग रद्द करनी है", language="hi", min_words=8)
    assert not should_skip_cleanup(MR_HOME + " आत्ताच", language="mr", min_words=8)


def test_english_skip_behaviour_is_unchanged():
    assert should_skip_cleanup("Meet me at noon", language="en", min_words=8)
    assert should_skip_cleanup("Meet me at noon tomorrow")


def test_scaled_min_words_preserves_never_skip():
    """A threshold of 0 means "always run cleanup"; scaling must not turn it
    into 1, which would start skipping single-word utterances."""
    assert languages.scaled_min_words(0, "hi") == 0
    assert languages.scaled_min_words(0, "en") == 0


def test_scaled_min_words_leaves_unscaled_languages_alone():
    assert languages.scaled_min_words(8, "en") == 8
    assert languages.scaled_min_words(8, "es") == 8
    assert languages.scaled_min_words(8, None) == 8
    assert languages.scaled_min_words(8, "klingon") == 8


def test_scaled_min_words_never_returns_zero_for_a_positive_threshold():
    for lang in languages.CATALOGUE:
        assert languages.scaled_min_words(1, lang.code) >= 1


# -- per-language routing data -------------------------------------------------


def test_indic_languages_ask_for_the_accurate_stt_model():
    """Turbo decodes Marathi through a Hindi prior and corrupts it."""
    assert languages.get("mr").needs_accurate_stt
    assert languages.get("hi").needs_accurate_stt
    assert not languages.get("en").needs_accurate_stt


def test_indic_languages_allow_latin_loanwords():
    """"meeting" inside a Marathi sentence is correct output, not the model
    going off-script, and the guardrail has to know that."""
    assert "LATIN" in languages.loanword_scripts("mr")
    assert "LATIN" in languages.loanword_scripts("hi")
    assert languages.loanword_scripts("en") == frozenset()


def test_register_hints_do_not_pin_a_script():
    """Script is decided by the translate/preserve directives, which know
    whether anything is being translated. A register hint that also named a
    script contradicted them and transliterated romanized Hinglish."""
    for code in ("hi", "mr"):
        hint = languages.get(code).register_hint
        assert "Devanagari" not in hint
        assert "Latin script" in hint or "another script" in hint
