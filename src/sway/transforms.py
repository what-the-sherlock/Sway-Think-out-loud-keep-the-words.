"""Transforms: a reusable instruction that rewrites text.

Two ways to invoke one, and they cost very different things:

* **Over a selection**, by keyboard shortcut. Select text anywhere, press the
  transform's chord, and the selection is replaced. This reuses
  :mod:`sway.commands` -- the same system prompt and the same single chat
  completion -- but supplies the instruction from storage instead of
  transcribing a spoken one, so it makes *one* network call where a voice
  command makes two.
* **Spoken mid-dictation**, by phrase. The phrase is matched locally, excised
  from the transcript, and its instruction is appended to the cleanup request
  that was going to happen anyway. This is D8.3's rule, and it is not
  negotiable: **one model call, always.** Chaining a call per transform would
  multiply both latency and the free-tier quota this app has to live inside.

That is why :data:`Transform.instruction` is a single compact imperative line
written when the user saves the transform, not a paragraph generated at
request time. The model never sees the transform library -- only the
directives for transforms that actually fired, which is normally none.

Everything here is a pure function over dataclasses, with no SQLite, no
network, and no threading, so it is directly unit-testable per CLAUDE.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sway.cleanup import NO_EXPECTATION, OutputExpectation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sway.settings import Trigger

#: Ceilings, for the same reason :mod:`sway.settings` has them: to stop a
#: hand-edited file making the app unusable, not to second-guess real input.
MAX_TRANSFORMS = 100
MAX_DESCRIPTION_CHARS = 200
#: Raised twice: from the original 600 to 2000 to fit Prompt Engineer's
#: structured, multi-section instruction, and from 2000 to 8000 when a
#: user's own hand-written instruction (a detailed, rule-by-rule email
#: rewriter, ~5000 characters) turned out to legitimately need more room
#: than that. This path is always a single call over a selection -- there is
#: no per-utterance cost to a longer instruction the way there would be for
#: something evaluated on every dictation, and 8000 characters is still a
#: trivial fraction of any model's context window for a one-shot rewrite.
MAX_INSTRUCTION_CHARS = 8000
MAX_EXAMPLE_CHARS = 4000


@dataclass(frozen=True, slots=True)
class Transform:
    """One reusable rewriting instruction."""

    id: str
    name: str
    #: The directive sent to the model. One imperative line: it is concatenated
    #: into a prompt alongside the cleanup rules, and a paragraph here is a
    #: paragraph on every request that fires it.
    instruction: str
    #: Shown in the UI only. Never sent to the model.
    description: str = ""
    #: Optional spoken trigger, matched mid-dictation. Empty means the
    #: transform is shortcut-only.
    phrase: str = ""
    #: Every keyboard/mouse binding that runs this transform over the current
    #: selection, as canonical :func:`sway.hotkey.parse_binding` strings.
    #: Empty means it has no shortcut and can only fire by ``phrase``.
    shortcuts: tuple[str, ...] = ()
    #: A short label rendered next to the name. Cosmetic.
    icon: str = ""
    #: Kept for schema continuity with settings files written before a
    #: transform's runnability was determined by whether it has a binding or
    #: a phrase rather than by a toggle (see :func:`is_runnable`). New and
    #: migrated transforms are always ``True``; nothing in this build ever
    #: sets it back to ``False``.
    enabled: bool = True
    #: True for the transforms Sway ships. A built-in can be edited and
    #: deleted like any other; the flag only drives "reset to defaults".
    builtin: bool = False
    #: A short "before" sample shown in the editor, illustrating what this
    #: transform does. Empty for a user-created transform until they try it.
    example_input: str = ""
    #: The corresponding "after" sample. Empty unless example_input is too.
    example_output: str = ""

    # -- output expectations -------------------------------------------------
    # What this transform is *allowed* to do to the text, so the cleanup
    # guardrails can tell "the model did what the user asked" apart from "the
    # model went off-script". Without these, the guardrails reject a correct
    # result: a bulleted rewrite trips the markdown check, a summary trips the
    # shrink floor, and an email trips the growth ceiling -- which is exactly
    # the bug this field set exists to fix.
    #
    # Deliberately three coarse booleans rather than raw ratio numbers. They
    # are checkboxes a user can answer honestly about their own instruction
    # ("does this produce a list?"); a float would be a number nobody can pick
    # correctly. All default False, so a transform that says nothing keeps the
    # full-strength guardrails. `may_shorten` is the one flag that also relaxes
    # the entity guard (see OutputExpectation.protects_entities): a genuine
    # summary of a long document is expected to drop most of its numbers and
    # dates, and a transform the user explicitly wrote and ticked "makes the
    # text shorter" for has already said that is the point.

    #: The instruction asks for a list, heading, or other markdown structure.
    allows_markdown: bool = False
    #: The instruction is expected to remove content (summarize, condense).
    may_shorten: bool = False
    #: The instruction is expected to add scaffolding (a greeting, a sign-off).
    may_lengthen: bool = False


#: Shipped transforms. Unlike the checkbox-era set these ship ready to run --
#: each has a shortcut, so presence in this list is what makes a transform
#: available, not a separate on/off switch (see :func:`is_runnable`).
#:
#: Each instruction states the constraint as well as the goal. "Make it
#: concise" alone invites an 8B-class model to summarize away half the
#: content; "without dropping any fact" is what stops it.
DEFAULT_TRANSFORMS: tuple[Transform, ...] = (
    Transform(
        id="prompt_engineer",
        name="Prompt Engineer",
        description="Turns a rough spoken idea into a structured AI prompt.",
        instruction=(
            "Rewrite the speaker's rough, spoken idea into a well-structured prompt "
            "for an AI model. Produce, in this exact order, using markdown "
            "'**heading**' lines: **Title** (one concise line naming the task); "
            "**Role & stance** (who the model should act as and how it should "
            "behave); **Task** (exactly what the model must do, one or two "
            "sentences); **Context** (only the background the model actually needs, "
            "drawn from what was said); **Inputs available** (what the model has to "
            "work with, as a short list); **Execution checklist** (2-5 short "
            "checklist items, each on its own line starting with '- [ ] ', capturing "
            "the constraints and must-dos implied by the request). Infer reasonable "
            "specifics from what was said rather than leaving placeholders, but never "
            "invent a fact, name, or number that was not implied."
        ),
        allows_markdown=True,
        may_lengthen=True,
        phrase="make that a prompt",
        shortcuts=("ctrl+alt+1",),
        example_input="design a better UI",
        example_output=(
            "**Title**\n"
            "Design a Better UI\n\n"
            "**Role & stance**\n"
            "You are a UI/UX designer and creative problem-solver.\n\n"
            "**Task**\n"
            "Create an improved user interface design for the specified application, "
            "emphasizing usability, aesthetics, and accessibility.\n\n"
            "**Context**\n"
            "[Provide a brief description of the current UI, target audience, "
            "platform, and any specific goals or constraints.]\n\n"
            "**Inputs available**\n"
            "- Current UI screenshots or description\n"
            "- Target audience and platform\n"
            "- Design goals or constraints\n\n"
            "**Execution checklist**\n"
            "- [ ] Improve visual hierarchy and usability\n"
            "- [ ] Maintain accessibility standards\n"
            "- [ ] Keep the redesign consistent with the existing brand"
        ),
        builtin=True,
    ),
    Transform(
        id="email",
        name="Turn into an email",
        description="Adds a greeting and a sign-off.",
        instruction=(
            "Rewrite as a short email with a greeting and a sign-off. Do not invent "
            "a recipient name, a sender name, or any fact the text does not contain."
        ),
        may_lengthen=True,
        phrase="make that an email",
        shortcuts=("ctrl+alt+2",),
        example_input="let the team know the deploy went fine and we're monitoring for the next hour",
        example_output=(
            "Hi team,\n\n"
            "The deploy went fine and we're monitoring it for the next hour.\n\n"
            "Best,"
        ),
        builtin=True,
    ),
    Transform(
        id="summarize",
        name="Summarize",
        description="The gist, in a couple of sentences.",
        instruction=(
            "Summarize in at most three sentences, keeping every decision, name, "
            "number, and date. This is the one case where losing detail is expected."
        ),
        may_shorten=True,
        phrase="summarize that",
        shortcuts=("ctrl+alt+3",),
        example_input=(
            "We discussed the Q3 roadmap for about an hour. Sarah's going to own the "
            "billing migration, Tom will handle the API versioning work, and we agreed "
            "to push the mobile redesign to Q4 since design isn't ready yet."
        ),
        example_output=(
            "On the Q3 roadmap, Sarah owns the billing migration, Tom owns API "
            "versioning, and the mobile redesign is pushed to Q4 pending design."
        ),
        builtin=True,
    ),
    Transform(
        id="professional",
        name="Rewrite professionally",
        description="For email and anything a client reads.",
        instruction=(
            "Rewrite in a professional register: full sentences, no slang, no "
            "contractions. Keep the meaning and every specific detail exactly."
        ),
        phrase="make that professional",
        shortcuts=("ctrl+alt+4",),
        example_input="hey so i think the deadline is kind of tight can we maybe push it a few days",
        example_output=(
            "I believe the current deadline is tight. Would it be possible to "
            "extend it by a few days?"
        ),
        builtin=True,
    ),
    Transform(
        id="polish",
        name="Polish",
        description="Tighten the wording without changing the voice.",
        instruction=(
            "Tidy the phrasing: remove padding and repetition, and smooth awkward "
            "sentences. Keep the author's voice, register, and every fact; do not "
            "shorten by removing content."
        ),
        phrase="polish that",
        shortcuts=("ctrl+alt+5",),
        example_input=(
            "So basically what I'm trying to say is that, um, we should probably go "
            "ahead and just, you know, finalize the release notes before we ship on "
            "Friday."
        ),
        example_output="We should finalize the release notes before we ship on Friday.",
        builtin=True,
    ),
    Transform(
        id="concise",
        name="Make concise",
        description="Say the same thing in fewer words.",
        instruction=(
            "Say the same thing in noticeably fewer words. Every fact, name, number, "
            "and request in the original must survive; cut words, not content."
        ),
        may_shorten=True,
        phrase="make that concise",
        shortcuts=("ctrl+alt+6",),
        example_input=(
            "I think we need to go back and look at the whole onboarding flow again "
            "because a lot of new users are dropping off somewhere around step three "
            "and we're not totally sure why yet."
        ),
        example_output=(
            "We need to revisit the onboarding flow -- many new users drop off around "
            "step three, and we don't yet know why."
        ),
        builtin=True,
    ),
    Transform(
        id="clarity",
        name="Improve clarity",
        description="Untangle it without shortening it.",
        instruction=(
            "Make the meaning easier to follow: split run-on sentences, put the "
            "point first, and replace vague references with what they refer to. "
            "Change no facts and drop no content."
        ),
        phrase="make that clearer",
        shortcuts=("ctrl+alt+7",),
        example_input=(
            "So the thing is, when the user does that thing where they click the "
            "button twice really fast, it kind of breaks because of how we handle it "
            "on the backend, which we should probably fix."
        ),
        example_output=(
            "Double-clicking the button breaks the flow because of how the backend "
            "handles the request. We should fix this."
        ),
        builtin=True,
    ),
)

DEFAULT_IDS = frozenset(t.id for t in DEFAULT_TRANSFORMS)

#: Expectation flags for transforms that used to ship as defaults but no
#: longer do (Fix grammar, Make casual, Turn into bullet points -- retired
#: when the shipped set shrank to the seven above). Migration never deletes
#: an existing user's copy of one of these; only "reset to defaults" stops
#: restoring it. :func:`shipped_expectation` is what keeps
#: settings._expectation_flag correct for such a transform even though it is
#: no longer in :data:`DEFAULT_TRANSFORMS` -- without this, an old
#: settings.json's "bullets" entry would silently lose ``allows_markdown``
#: and reintroduce the exact bug that flag exists to prevent. Add to this
#: only when retiring a default that had a non-default expectation flag;
#: never use it to give a *new* transform its flags.
_RETIRED_DEFAULT_EXPECTATIONS: dict[str, dict[str, bool]] = {
    "bullets": {"allows_markdown": True, "may_shorten": True},
}


def shipped_expectation(transform_id: str, field: str) -> bool:
    """The expectation flag a *shipped* transform of this id declares, past
    or present: the current defaults first, then transforms retired from
    them. ``False`` for any id that was never shipped."""
    current = by_id(DEFAULT_TRANSFORMS, transform_id)
    if current is not None:
        return bool(getattr(current, field))
    return bool(_RETIRED_DEFAULT_EXPECTATIONS.get(transform_id, {}).get(field, False))


def by_id(transforms: Iterable[Transform], transform_id: str) -> Transform | None:
    for transform in transforms:
        if transform.id == transform_id:
            return transform
    return None


def is_runnable(transform: Transform) -> bool:
    """Whether this transform can fire at all.

    Presence of a binding or a phrase *is* the on switch now -- there is no
    separate toggle to check (see :attr:`Transform.enabled`'s docstring). A
    transform with neither shows in the UI as "no shortcut or phrase yet" and
    simply never matches or dispatches.
    """
    return bool(transform.shortcuts) or bool(transform.phrase.strip())


def enabled_with_phrase(transforms: Iterable[Transform]) -> list[Transform]:
    """Transforms that can fire from speech: runnable, and with a phrase."""
    return [t for t in transforms if t.phrase.strip()]


def enabled_with_shortcut(transforms: Iterable[Transform]) -> list[Transform]:
    """Transforms that can fire from the keyboard: runnable, and bound."""
    return [t for t in transforms if t.shortcuts]


#: The trigger type a transform is matched as. Not in
#: :data:`sway.settings.TRIGGER_TYPES`, deliberately: a user cannot create a
#: "transform trigger" by hand, because transforms are their own list with
#: their own fields. This type only exists on the records built below.
MATCH_TYPE = "transform"


def as_matchable(transforms: Iterable[Transform]) -> list[Trigger]:
    """Enabled, phrase-carrying transforms as :class:`~sway.settings.Trigger`
    records, so one matcher serves snippets and transforms alike.

    Structural, not stored: these are built per utterance and never written to
    settings.json. What it buys is that a transform phrase inherits every
    false-positive guard the snippet matcher already has -- whole-word
    matching, longest-phrase-first, non-overlapping spans -- rather than a
    second implementation of them that drifts.
    """
    from sway.settings import Trigger

    return [
        Trigger(
            id=t.id,
            name=t.name,
            phrase=t.phrase,
            payload="",
            type=MATCH_TYPE,
            enabled=True,
        )
        for t in enabled_with_phrase(transforms)
    ]


def expectation_for(fired: Sequence[Transform]) -> OutputExpectation:
    """The merged output expectation of the transforms that fired.

    Permissive across the set, because every fired instruction is one the user
    asked for: "summarize that, and make that bullet points" is entitled to
    both shrink past the ordinary floor and contain bullets. Returns the
    full-strength :data:`~sway.cleanup.NO_EXPECTATION` when nothing fired,
    which is the common case and leaves plain dictation's guardrails exactly
    as they were.

    Kept beside :func:`build_directive_block` on purpose: these are the two
    halves of one idea -- what the model is told to do, and what the guardrails
    must therefore let it do -- and a transform that gains a directive without
    gaining the matching expectation is precisely the bug that made "make that
    bullet points" silently paste unbulleted text.
    """
    if not fired:
        return NO_EXPECTATION
    return OutputExpectation(
        allows_markdown=any(t.allows_markdown for t in fired),
        may_shorten=any(t.may_shorten for t in fired),
        may_lengthen=any(t.may_lengthen for t in fired),
    )


def build_directive_block(fired: Sequence[Transform]) -> str:
    """The directive text appended to a cleanup request for the transforms that
    fired.

    Returns ``""`` for the overwhelmingly common case of nothing having fired,
    which is what keeps an ordinary dictation's prompt byte-identical across
    requests and therefore cacheable.

    The precedence line is D8.4's instruction hierarchy stated to the model
    rather than merely assumed, so "make it terse" and "add more detail" have a
    defined outcome instead of whichever the model noticed last. Order is the
    order the phrases were spoken.
    """
    directives = [t.instruction.strip() for t in fired if t.instruction.strip()]
    if not directives:
        return ""

    if len(directives) == 1:
        body = directives[0]
    else:
        numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(directives, start=1))
        body = (
            "Apply all of the following. Where two of them conflict, the "
            f"earlier one wins.\n{numbered}"
        )
    return (
        " The speaker also asked for the text to be changed in a specific way. "
        "Apply this after the cleanup rules above, and still never answer, "
        "summarize beyond what is asked, or add commentary: " + body
    )


def find_shortcut_conflicts(transforms: Sequence[Transform]) -> dict[str, list[str]]:
    """Map transform id -> ids of other transforms sharing any one binding.

    Reported symmetrically, as :func:`sway.triggers.find_conflicts` is, so the
    UI can flag both rows rather than arbitrarily blaming one. A transform
    with several bindings conflicts with another the moment *any one* of them
    is shared -- the shared binding would only ever reach whichever transform
    the underlying listener happens to dispatch it to.
    """
    conflicts: dict[str, list[str]] = {}
    for outer in transforms:
        outer_set = set(outer.shortcuts)
        if not outer_set:
            continue
        for inner in transforms:
            if inner.id != outer.id and outer_set & set(inner.shortcuts):
                conflicts.setdefault(outer.id, []).append(inner.id)
    return conflicts


__all__ = [
    "DEFAULT_IDS",
    "DEFAULT_TRANSFORMS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_EXAMPLE_CHARS",
    "MAX_INSTRUCTION_CHARS",
    "MATCH_TYPE",
    "MAX_TRANSFORMS",
    "Transform",
    "as_matchable",
    "build_directive_block",
    "by_id",
    "enabled_with_phrase",
    "enabled_with_shortcut",
    "expectation_for",
    "find_shortcut_conflicts",
    "is_runnable",
    "shipped_expectation",
]
