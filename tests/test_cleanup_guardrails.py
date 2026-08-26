"""Tests for the cleanup safety layer: output normalization, the guardrails
that decide whether a rewrite may be pasted at all, and the disfluency
signals that decide whether to spend a cleanup call in the first place.

Pure functions only, per CLAUDE.md: no network involved. Several of these
encode failures observed end-to-end against the live Groq API rather than
hypothetical ones -- see the comments on the individual tests.
"""

from __future__ import annotations

import pytest

from sway.cleanup import (
    CLEANUP_SYSTEM_PROMPT,
    NO_EXPECTATION,
    SPOKEN_COMMAND_EXPECTATION,
    OutputExpectation,
    has_filler_markers,
    is_cleanup_output_safe,
    normalize_cleanup_output,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from sway.profiles import CleanupStyle
from sway.transforms import (
    DEFAULT_TRANSFORMS,
    Transform,
    by_id,
    expectation_for,
    shipped_expectation,
)

# Hindi fixtures, spelled out so the file stays readable in any editor.
HI_TOMORROW_TEN = "कल सुबह दस बजे ठीक रहेगा"
HI_TOMORROW_TEN_CLEAN = "कल सुबह दस बजे ठीक रहेगा।"


# -- self-correction detection ----------------------------------------------
# The failure this catches was observed end-to-end: "Let's meet Friday, sorry,
# Thursday at 4." is seven words with no "um", so the word-count heuristic
# skipped cleanup and shipped the retracted day to the user's cursor.


@pytest.mark.parametrize(
    "text",
    [
        "Let's meet Friday, sorry, Thursday at 4.",
        "Send it to Anna - no wait, to Priya.",
        "The total is 40, I meant 45.",
        "Book the 9am - actually no, the 10am.",
        "Ship it Tuesday. Scratch that, Wednesday.",
    ],
)
def test_self_corrections_are_detected_as_disfluency(text: str):
    assert has_filler_markers(text)
    assert not should_skip_cleanup(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm sorry about the delay.",  # a sincere apology, not a correction
        "I actually finished it yesterday.",  # "actually" as emphasis
        "Rather than that, use the other one.",
    ],
)
def test_sincere_phrasing_is_not_a_self_correction(text: str):
    assert not has_filler_markers(text)


# -- output normalization ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Here is the cleaned-up text: Let's meet at three.", "Let's meet at three."),
        ("Sure, here's the cleaned up version: Ship it today.", "Ship it today."),
        ("Cleaned-up text: Ship it today.", "Ship it today."),
        ('"Ship it today."', "Ship it today."),
        ("“Ship it today.”", "Ship it today."),
        ("```\nShip it today.\n```", "Ship it today."),
        ("```text\nShip it today.\n```", "Ship it today."),
        ("Ship it today.", "Ship it today."),
    ],
)
def test_normalize_strips_model_wrappers(raw: str, expected: str):
    assert normalize_cleanup_output(raw, "ship it today") == expected


def test_normalize_keeps_quotes_the_speaker_actually_dictated():
    original = '"the whole thing is quoted"'
    cleaned = '"The whole thing is quoted."'
    assert normalize_cleanup_output(cleaned, original) == cleaned


# -- guardrails: data the cleanup pass must never alter -----------------------


@pytest.mark.parametrize(
    "original,cleaned",
    [
        # a number silently changed
        ("send the 450 pound invoice today please", "Send the 460 pound invoice today, please."),
        # an email address "tidied"
        (
            "email rahul.sharma@acme.com about the delay tomorrow",
            "Email rahul@acme.com about the delay tomorrow.",
        ),
        # a URL truncated
        (
            "the docs are at https://example.com/a/b/c go and read them",
            "The docs are at https://example.com and you should read them.",
        ),
        # a file path paraphrased away
        (
            "open the config at /etc/nginx/nginx.conf and restart it",
            "Open the config at the nginx directory and restart it.",
        ),
    ],
)
def test_altered_user_data_is_rejected(original: str, cleaned: str):
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned).startswith("lost_entity")


def test_preserved_user_data_passes():
    original = "um email the invoice to rahul.sharma@acme.com its 450 pounds due on the 12th"
    cleaned = "Email the invoice to rahul.sharma@acme.com. It's 450 pounds, due on the 12th."
    assert is_cleanup_output_safe(original, cleaned)


def test_currency_symbol_added_around_a_preserved_number_is_fine():
    original = "the invoice is 450 pounds and it is due next week sometime"
    assert is_cleanup_output_safe(original, "The invoice is £450 and it is due next week.")


# -- guardrails: translation -------------------------------------------------


def test_translating_a_transcript_away_is_rejected():
    cleaned = "Ten o'clock tomorrow morning works."
    assert not is_cleanup_output_safe(HI_TOMORROW_TEN, cleaned)
    assert unsafe_reason(HI_TOMORROW_TEN, cleaned) == "language_changed"


def test_cleaning_within_the_same_script_is_allowed():
    original = "कल कल सुबह दस बजे ठीक रहेगा"
    assert is_cleanup_output_safe(original, HI_TOMORROW_TEN_CLEAN)


def test_mixed_language_speech_keeps_both_scripts():
    original = f"can you send this to Rahul {HI_TOMORROW_TEN}"
    good = f"Can you send this to Rahul? {HI_TOMORROW_TEN_CLEAN}"
    dropped_hindi = "Can you send this to Rahul tomorrow morning at ten?"
    assert is_cleanup_output_safe(original, good)
    assert not is_cleanup_output_safe(original, dropped_hindi)


# -- guardrails: markdown and assistant voice --------------------------------


@pytest.mark.parametrize(
    "cleaned",
    [
        "- Buy milk\n- Buy eggs\n- Buy bread",
        "# Shopping list\n\nBuy milk, eggs and bread today",
        "Buy **milk**, eggs and bread at the store today",
        "1. Buy milk\n2. Buy eggs\n3. Buy bread",
        "```\nbuy milk eggs and bread\n```",
    ],
)
def test_invented_markdown_is_rejected(cleaned: str):
    original = "buy milk eggs and bread at the store today please"
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == "markdown"


def test_markdown_the_speaker_dictated_is_allowed():
    original = "- buy milk\n- buy eggs\n- buy bread and some cheese"
    cleaned = "- Buy milk\n- Buy eggs\n- Buy bread and some cheese."
    assert is_cleanup_output_safe(original, cleaned)


@pytest.mark.parametrize(
    "cleaned",
    [
        "As an AI, I can't help with scheduling meetings for you today.",
        "The transcript says the user wants to meet on Thursday afternoon.",
        "Sure, here is the text you asked me to clean up for you today.",
    ],
)
def test_assistant_voice_is_rejected(cleaned: str):
    original = "lets meet on thursday afternoon if that works for everyone"
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == "assistant_voice"


def test_ordinary_first_person_speech_is_not_assistant_voice():
    # The regression that motivated narrowing the pattern: a legitimate
    # formal expansion of "cant make it" must survive.
    assert is_cleanup_output_safe("cant make it dont wait for me", "I cannot make it. Do not wait.")


def test_answering_a_dictated_question_is_rejected():
    original = "what is the capital of france anyway do you know"
    cleaned = "The capital of France is Paris, one of the largest cities."
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == "answered_question"


def test_a_question_that_stays_a_question_is_fine():
    original = "um so what is the plan for thursday do you know yet"
    assert is_cleanup_output_safe(original, "So what is the plan for Thursday, do you know yet?")


def test_removing_a_spurious_question_mark_is_not_treated_as_answering():
    # Observed live: Whisper punctuated this long statement as a question,
    # and the correct rewrite dropped the "?". Rejecting that would have
    # thrown away a good cleanup over Whisper's own mistake. The rewrite is
    # built almost entirely from the speaker's own words, so it cannot be an
    # answer to them.
    original = (
        "Okay so, um, I was thinking about the roadmap for next quarter, and I think we "
        "should, uh, we should really focus on the onboarding flow first, because, you "
        "know, the drop off there is like 40%, which is, I mean, that's clearly the "
        "biggest problem we have right now, and everything else can honestly wait until "
        "we fix it?"
    )
    cleaned = (
        "Okay, I was thinking about the roadmap for next quarter, and I think we should "
        "really focus on the onboarding flow first, because the drop off there is like "
        "40%, which is clearly the biggest problem we have right now, and everything "
        "else can honestly wait until we fix it."
    )
    assert is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == ""


def test_unsafe_reason_is_empty_for_a_good_rewrite():
    assert unsafe_reason("um we should go to the store", "We should go to the store.") == ""


# -- language-aware system prompt --------------------------------------------


def test_system_prompt_pins_the_detected_language():
    prompt = system_prompt_for_style(CleanupStyle.CASUAL, language="hindi")
    assert "reply in hindi" in prompt.lower()


def test_system_prompt_without_a_language_stays_agnostic():
    prompt = system_prompt_for_style(CleanupStyle.CASUAL, language=None)
    assert prompt == CLEANUP_SYSTEM_PROMPT
    assert "primary language is" not in prompt


def test_every_style_prompt_forbids_translating_and_markdown():
    for style in (CleanupStyle.CASUAL, CleanupStyle.FORMAL):
        prompt = system_prompt_for_style(style)
        assert "Never translate" in prompt
        assert "no markdown" in prompt


# ---------------------------------------------------------------------------
# The correction exemption
#
# The entity guard treats every number in a transcript as protected. That is
# right for a tidy-up and wrong for a retraction: "a meeting at 7am, no no,
# it's at 8am" *must* lose the 7 to be correct, and without an exemption the
# right answer is indistinguishable from the model dropping a figure. These
# tests pin both halves -- that the exemption works, and that it stays narrow.
# ---------------------------------------------------------------------------


def test_a_retracted_number_may_disappear():
    raw = "I have a meeting at 7am, no no, it's at 8am."
    cleaned = "I have a meeting at 8am."
    assert not is_cleanup_output_safe(raw, cleaned)
    assert is_cleanup_output_safe(
        raw, cleaned, retracted="7am, no no", correction_detected=True
    )


def test_a_retracted_time_may_disappear():
    raw = "Call me at 10:30 - make that 11."
    cleaned = "Call me at 11."
    assert unsafe_reason(raw, cleaned) == "lost_entity:10:30"
    assert is_cleanup_output_safe(
        raw, cleaned, retracted="10:30 - make that", correction_detected=True
    )


def test_the_exemption_does_not_cover_untouched_entities():
    """A correction licenses losing what the speaker took back, and nothing
    else. The room number was never retracted and must survive."""
    raw = "Meeting at 7am, no no, at 8am, in room 412."
    assert is_cleanup_output_safe(
        raw, "Meeting at 8am in room 412.", retracted="7am, no no", correction_detected=True
    )
    assert not is_cleanup_output_safe(
        raw, "Meeting at 8am.", retracted="7am, no no", correction_detected=True
    )


def test_the_exemption_is_off_by_default():
    """Every existing caller passes neither argument, so the defaults must
    reproduce the old behaviour exactly."""
    raw = "The invoice is 4471 and the total is 320."
    cleaned = "The invoice is 4471."
    assert not is_cleanup_output_safe(raw, cleaned)
    assert unsafe_reason(raw, cleaned) == "lost_entity:320"


def test_a_correction_lowers_the_shrink_floor():
    """A long, stumbling retraction correctly collapses to a short sentence.
    Twenty words becoming five is a ratio the ordinary floor rejects outright,
    and rejecting it means pasting the whole stumble at the user's cursor."""
    raw = (
        "The budget for this whole project is five thousand pounds, no wait, "
        "sorry, scratch that, make it fifty thousand pounds"
    )
    cleaned = "The budget is fifty thousand."
    assert not is_cleanup_output_safe(raw, cleaned)
    assert unsafe_reason(raw, cleaned) == "shrank"
    assert is_cleanup_output_safe(raw, cleaned, correction_detected=True)


def test_the_lowered_floor_still_rejects_a_summary():
    """Relaxing the floor must not open the door to the model summarizing."""
    raw = (
        "So the plan is that we ship the beta on Tuesday, gather feedback for a "
        "fortnight, and then decide whether the general release slips to the "
        "following month or stays where it is on the roadmap."
    )
    assert not is_cleanup_output_safe(raw, "We ship Tuesday.", correction_detected=True)


def test_unsafe_reason_agrees_with_the_check_under_a_correction():
    """A rejection with no stated cause would be unloggable, so the two
    functions have to stay in step under every argument combination."""
    cases = [
        ("Meeting at 7am, no no, at 8am, in room 412.", "Meeting at 8am.", "7am, no no"),
        ("I have a meeting at 7am, no no, it's at 8am.", "I have a meeting at 8am.", "7am, no no"),
        ("Send it to a@b.com, sorry, c@d.com.", "Send it to c@d.com.", "a@b.com, sorry"),
    ]
    for raw, cleaned, retracted in cases:
        safe = is_cleanup_output_safe(
            raw, cleaned, retracted=retracted, correction_detected=True
        )
        reason = unsafe_reason(raw, cleaned, retracted=retracted, correction_detected=True)
        assert safe == (reason == "")


def test_the_correction_directive_is_only_added_when_asked():
    plain = system_prompt_for_style(CleanupStyle.CASUAL)
    with_correction = system_prompt_for_style(CleanupStyle.CASUAL, correction=True)
    assert "corrected themselves somewhere" not in plain
    assert "corrected themselves somewhere" in with_correction
    # The stable half must stay byte-identical and stay first, or Groq's
    # prompt caching has nothing to reuse across requests.
    assert with_correction.startswith(plain)


def test_the_correction_directive_teaches_the_negative_cases():
    """The examples are the specification. Without the two pass-through cases
    the model over-applies the instruction and "I don't want to go, actually"
    loses its negation."""
    prompt = system_prompt_for_style(CleanupStyle.CASUAL, correction=True)
    assert prompt.count("I don't want to go, actually.") == 2
    assert prompt.count("We shipped Monday and, actually, Tuesday as well.") == 2


# -- transform-aware guardrails ---------------------------------------------
# The failure these catch was observed end-to-end and is in the user's own log:
#
#   transform(s) fired: Turn into bullet points
#   cleanup output rejected by the markdown guardrail; pasting the raw
#   transcript instead
#
# The transform matched, the directive reached the model, and the model
# returned a correct bulleted list -- which the markdown guardrail then threw
# away, because it had no way to know bullets were the point. Every case below
# asserts both halves: the output is accepted *with* the transform's
# expectation, and still rejected without it.


def _expect(transform_id: str) -> OutputExpectation:
    """The shipped expectation for one built-in, current or retired.

    Goes through shipped_expectation (not a direct DEFAULT_TRANSFORMS
    lookup) specifically so "bullets" -- retired from the shipped set, but
    still a live case for any existing user's settings.json -- keeps
    exercising exactly the same guardrail scenario it always has.
    """
    return OutputExpectation(
        allows_markdown=shipped_expectation(transform_id, "allows_markdown"),
        may_shorten=shipped_expectation(transform_id, "may_shorten"),
        may_lengthen=shipped_expectation(transform_id, "may_lengthen"),
    )


# Representative model output for each transform, written the way the shipped
# instruction actually asks for it.
GROCERIES = "I want to order groceries like Kurkure, namkeen, potatoes and sugar"
GROCERIES_BULLETED = "- Kurkure\n- Namkeen\n- Potatoes\n- Sugar"

STANDUP = (
    "so basically the deploy went out at nine this morning and then the database "
    "migration finished around ten which took a lot longer than any of us expected "
    "and then we found the caching bug in the checkout flow and had to roll the "
    "whole thing back at eleven, and Priya is going to pick it up again tomorrow "
    "morning and work out whether the invalidation logic is what actually broke it "
    "or whether it was something in the deploy script itself, anyway that is "
    "roughly where we have ended up"
)
STANDUP_SUMMARY = (
    "Deploy at nine, migration at ten, and a checkout caching bug forced a "
    "rollback at eleven. Priya picks it up tomorrow."
)

NOTE = "tell Ali the deck is ready"
NOTE_AS_EMAIL = (
    "Hi Ali,\n\nJust a quick note to let you know that the deck is ready whenever "
    "you want to take a look at it. Do let me know if anything needs "
    "changing.\n\nBest regards"
)


@pytest.mark.parametrize(
    "transform_id, original, cleaned",
    [
        ("bullets", GROCERIES, GROCERIES_BULLETED),
        ("summarize", STANDUP, STANDUP_SUMMARY),
        ("email", NOTE, NOTE_AS_EMAIL),
    ],
)
def test_a_transform_output_is_accepted_with_its_own_expectation(
    transform_id: str, original: str, cleaned: str
):
    assert unsafe_reason(original, cleaned, expectation=_expect(transform_id)) == ""
    assert is_cleanup_output_safe(original, cleaned, expectation=_expect(transform_id))


@pytest.mark.parametrize(
    "original, cleaned, reason",
    [
        (GROCERIES, GROCERIES_BULLETED, "markdown"),
        (STANDUP, STANDUP_SUMMARY, "shrank"),
        (NOTE, NOTE_AS_EMAIL, "grew"),
    ],
)
def test_the_same_output_is_still_rejected_with_no_transform(
    original: str, cleaned: str, reason: str
):
    """The relaxation must be scoped to a fired transform. Plain dictation
    cleanup has no business inventing bullets or summarizing, and this is what
    proves the fix did not simply switch the guardrails off."""
    assert unsafe_reason(original, cleaned) == reason
    assert not is_cleanup_output_safe(original, cleaned)


def test_a_markdown_only_expectation_does_not_relax_the_entity_guard():
    """Being allowed to restructure into a list is not a licence to drop what
    was actually said -- only an instruction that says it *shortens* the text
    is trusted with that (see test_may_shorten_relaxes_the_entity_guard)."""
    original = "order 6 packets of namkeen and 2 kilos of sugar"
    dropped = "- Namkeen\n- Sugar"
    markdown_only = OutputExpectation(allows_markdown=True)
    reason = unsafe_reason(original, dropped, expectation=markdown_only)
    assert reason.startswith("lost_entity:")


def test_may_shorten_relaxes_the_entity_guard():
    """A transform the user explicitly ticked "makes the text shorter" for is
    trusted to drop numbers and dates the way a real summary would -- see
    OutputExpectation.protects_entities. Reported end to end: a user's own
    Summarize rewrite of a long document was rejected outright because it did
    not repeat every figure in the source, which defeats the point of asking
    for a summary at all."""
    original = "order 6 packets of namkeen and 2 kilos of sugar"
    summarized = "Namkeen and sugar were ordered."
    shortening = OutputExpectation(may_shorten=True)
    assert unsafe_reason(original, summarized, expectation=shortening) == ""


def test_an_expectation_never_relaxes_the_assistant_voice_guard():
    original = GROCERIES
    chatty = "Sure! Here are your groceries:\n- Kurkure\n- Namkeen"
    assert unsafe_reason(original, chatty, expectation=_expect("bullets")) == "assistant_voice"


def test_an_expectation_never_relaxes_the_language_guard():
    """A Hindi dictation turned into a bulleted English list is a translation,
    not a transform."""
    original = HI_TOMORROW_TEN
    translated = "- Tomorrow at ten in the morning works"
    assert (
        unsafe_reason(original, translated, expectation=_expect("bullets"))
        == "language_changed"
    )


def test_expectations_merge_permissively_across_fired_transforms():
    """Two phrases in one utterance are two things the user asked for, so both
    relaxations apply -- "summarize that, make that bullet points" is entitled
    to shrink *and* to contain bullets."""
    merged = expectation_for(
        [by_id(DEFAULT_TRANSFORMS, "summarize"), by_id(DEFAULT_TRANSFORMS, "email")]
    )
    assert merged.allows_markdown is False
    assert merged.may_shorten is True
    assert merged.may_lengthen is True


def test_no_fired_transform_is_the_full_strength_default():
    assert expectation_for([]) is NO_EXPECTATION


def test_a_transform_with_no_declared_expectation_keeps_strict_guardrails():
    """A custom transform the user wrote says nothing about its output, so it
    is checked exactly as plain dictation is."""
    custom = Transform(id="mine", name="Mine", instruction="Do a thing", enabled=True)
    assert expectation_for([custom]) == NO_EXPECTATION
    assert unsafe_reason(GROCERIES, GROCERIES_BULLETED, expectation=expectation_for([custom])) == "markdown"


def test_polish_does_not_quietly_gain_permission_to_restructure():
    """Only the transforms whose instruction demands it are relaxed. Polish
    tidies wording, so bullets from Polish are still the model going
    off-script."""
    assert _expect("polish") == NO_EXPECTATION
    assert unsafe_reason(GROCERIES, GROCERIES_BULLETED, expectation=_expect("polish")) == "markdown"


def test_a_spoken_command_may_restructure_a_selection():
    """The speaker stated an instruction over text they selected, so the
    shape-based checks are relaxed, including the entity guard: SPOKEN_COMMAND_
    EXPECTATION carries may_shorten=True, and a spoken "summarize that" is
    entitled to drop numbers exactly as a saved Summarize transform is."""
    assert unsafe_reason(GROCERIES, GROCERIES_BULLETED, expectation=SPOKEN_COMMAND_EXPECTATION) == ""
    assert (
        unsafe_reason(
            "order 6 packets", "- packets", expectation=SPOKEN_COMMAND_EXPECTATION
        )
        == ""
    )


# -- the "grew" ceiling for a lengthening transform --------------------------
# Reported end to end: a real sentence run through the shipped "Turn into an
# email" transform was rejected as "grew" even though turning a sentence
# into a complete email is the entire point of the transform. The 60-word
# absolute allowance was sized for a one-line note's greeting-and-sign-off
# and never re-sized as longer may_lengthen transforms shipped -- see
# sway.cleanup._LENGTHENING_ABSOLUTE_WORD_GROWTH.

COMPLAINT = (
    "I want to complain to the government about the road issues. They are "
    "of very bad quality and we need good roads."
)
COMPLAINT_AS_EMAIL = (
    "Subject: Request for Urgent Repair of Deteriorating Road Conditions\n\n"
    "Dear Sir or Madam,\n\n"
    "I am writing to raise a concern about the condition of the roads in "
    "our area, which has deteriorated noticeably over recent months. The "
    "surfaces are in poor repair, with potholes and uneven sections that "
    "make daily travel difficult and, in places, unsafe for both drivers "
    "and pedestrians.\n\n"
    "I would be grateful if you could advise what steps are being taken "
    "to address this, and whether a timeline for repair work has been "
    "established. Improving these roads would make a meaningful "
    "difference to everyone who relies on them.\n\n"
    "Thank you for your time and attention to this matter.\n\n"
    "Yours faithfully"
)


def test_the_reported_sentence_becomes_a_real_email():
    assert unsafe_reason(COMPLAINT, COMPLAINT_AS_EMAIL, expectation=_expect("email")) == ""
    assert unsafe_reason(COMPLAINT, COMPLAINT_AS_EMAIL) == "grew"


def test_a_short_idea_becomes_a_full_prompt_scaffold():
    """prompt_engineer's own shipped example: a few words in, a full
    Title/Role/Task/Context/Inputs scaffold out. Rejected before the fix."""
    prompt_engineer = by_id(DEFAULT_TRANSFORMS, "prompt_engineer")
    assert prompt_engineer is not None
    assert prompt_engineer.example_input and prompt_engineer.example_output
    reason = unsafe_reason(
        prompt_engineer.example_input,
        prompt_engineer.example_output,
        expectation=_expect("prompt_engineer"),
    )
    assert reason == ""


def test_a_runaway_rewrite_is_still_rejected_under_may_lengthen():
    """The absolute allowance rescuing short input must not become a licence
    for the model to answer/expound at any length. A 100-word selection
    becoming a 1200-word essay trips the ratio ceiling regardless."""
    original = " ".join(f"word{i}" for i in range(100))
    runaway = " ".join(f"filler{i}" for i in range(1200))
    assert unsafe_reason(original, runaway, expectation=_expect("email")) == "grew"


@pytest.mark.parametrize("transform", [t for t in DEFAULT_TRANSFORMS if t.example_output])
def test_every_shipped_transform_example_passes_its_own_expectation(transform: Transform):
    """The test that would have caught this class of bug directly: every
    transform's own shipped, UI-visible example must survive the guardrail
    under that transform's own expectation."""
    reason = unsafe_reason(
        transform.example_input, transform.example_output, expectation=_expect(transform.id)
    )
    assert reason == "", f"{transform.id}: {reason}"


def test_a_plain_subject_line_is_not_markdown():
    """email declares allows_markdown=False deliberately -- an ordinary
    "Subject: ..." line and paragraph breaks must not trip it, since a real
    email always has both."""
    assert unsafe_reason(COMPLAINT, COMPLAINT_AS_EMAIL, expectation=_expect("email")) == ""


def test_a_bold_subject_line_trips_the_markdown_guard():
    """The genuine exposure of allows_markdown=False on email: the model
    reaching for **bold** or a bulleted body is still caught, which is the
    guard doing its job -- an email transform is not licence to invent
    structure the user did not ask for."""
    bolded = COMPLAINT_AS_EMAIL.replace("Subject:", "**Subject:**")
    assert unsafe_reason(COMPLAINT, bolded, expectation=_expect("email")) == "markdown"


def test_is_safe_and_unsafe_reason_cannot_disagree():
    """They were parallel implementations of the same rules; a rejection whose
    reason came back "" would log a fallback with no cause."""
    cases = [
        (GROCERIES, GROCERIES_BULLETED, NO_EXPECTATION),
        (GROCERIES, GROCERIES_BULLETED, _expect("bullets")),
        (STANDUP, STANDUP_SUMMARY, _expect("summarize")),
        (NOTE, NOTE_AS_EMAIL, NO_EXPECTATION),
        (GROCERIES, "", NO_EXPECTATION),
    ]
    for original, cleaned, expectation in cases:
        reason = unsafe_reason(original, cleaned, expectation=expectation)
        assert is_cleanup_output_safe(original, cleaned, expectation=expectation) == (reason == "")
