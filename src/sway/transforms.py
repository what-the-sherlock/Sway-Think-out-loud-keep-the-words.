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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sway.settings import Trigger

#: Ceilings, for the same reason :mod:`sway.settings` has them: to stop a
#: hand-edited file making the app unusable, not to second-guess real input.
MAX_TRANSFORMS = 100
MAX_DESCRIPTION_CHARS = 200
MAX_INSTRUCTION_CHARS = 600


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
    #: Optional chord, e.g. "ctrl+alt+p". Empty means no shortcut.
    shortcut: str = ""
    #: A short label rendered next to the name. Cosmetic.
    icon: str = ""
    enabled: bool = False
    #: True for the transforms Sway ships. A built-in can be edited and
    #: disabled like any other; the flag only drives "reset to defaults".
    builtin: bool = False


#: Shipped transforms. **All disabled by default**, per D8.5: a transform costs
#: a model call and free-tier quota, so nothing here starts spending either
#: until the user turns it on.
#:
#: Each instruction is one line and states the constraint as well as the goal.
#: "Make it concise" alone invites an 8B-class model to summarize away half the
#: content; "without dropping any fact" is what stops it.
DEFAULT_TRANSFORMS: tuple[Transform, ...] = (
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
        phrase="make that concise",
        builtin=True,
    ),
    Transform(
        id="grammar",
        name="Fix grammar",
        description="Correct mistakes and nothing else.",
        instruction=(
            "Correct grammar, spelling, and punctuation only. Do not reword, "
            "reorder, or restyle anything that was already correct."
        ),
        phrase="fix the grammar",
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
        builtin=True,
    ),
    Transform(
        id="casual",
        name="Make casual",
        description="Loosen it up for chat.",
        instruction=(
            "Rewrite in a relaxed, conversational register, as if writing to a "
            "colleague you know well. Keep the meaning and every specific detail."
        ),
        phrase="make that casual",
        builtin=True,
    ),
    Transform(
        id="bullets",
        name="Turn into bullet points",
        description="One point per line.",
        instruction=(
            "Rewrite as a bulleted list, one idea per bullet, each starting with "
            "'- '. Keep every point from the original and add none."
        ),
        phrase="make that bullet points",
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
        phrase="make that an email",
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
        phrase="summarize that",
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
        builtin=True,
    ),
)

DEFAULT_IDS = frozenset(t.id for t in DEFAULT_TRANSFORMS)


def by_id(transforms: Iterable[Transform], transform_id: str) -> Transform | None:
    for transform in transforms:
        if transform.id == transform_id:
            return transform
    return None


def enabled_with_phrase(transforms: Iterable[Transform]) -> list[Transform]:
    """Transforms that can fire from speech: enabled, and with a phrase."""
    return [t for t in transforms if t.enabled and t.phrase.strip()]


def enabled_with_shortcut(transforms: Iterable[Transform]) -> list[Transform]:
    """Transforms that can fire from the keyboard: enabled, and bound."""
    return [t for t in transforms if t.enabled and t.shortcut.strip()]


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


def normalise_shortcut(shortcut: str) -> str:
    """The canonical form a chord is stored and compared in."""
    return "+".join(part.strip().lower() for part in shortcut.split("+") if part.strip())


def find_shortcut_conflicts(transforms: Sequence[Transform]) -> dict[str, list[str]]:
    """Map transform id -> ids of transforms bound to the same chord.

    Reported symmetrically, as :func:`sway.triggers.find_conflicts` is, so the
    UI can flag both rows rather than arbitrarily blaming one.
    """
    conflicts: dict[str, list[str]] = {}
    chords = {t.id: normalise_shortcut(t.shortcut) for t in transforms if t.shortcut.strip()}
    for outer, chord in chords.items():
        for inner, other in chords.items():
            if inner != outer and chord == other:
                conflicts.setdefault(outer, []).append(inner)
    return conflicts


__all__ = [
    "DEFAULT_IDS",
    "DEFAULT_TRANSFORMS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INSTRUCTION_CHARS",
    "MATCH_TYPE",
    "MAX_TRANSFORMS",
    "Transform",
    "as_matchable",
    "build_directive_block",
    "by_id",
    "enabled_with_phrase",
    "enabled_with_shortcut",
    "find_shortcut_conflicts",
    "normalise_shortcut",
]
