"""Detecting spoken self-corrections, so the cleanup pass can act on them.

This module is a **router, not a rewriter**. It never deletes a word. Deciding
that "I don't want to go, actually." is *not* a correction needs semantics, and
the model already has them; what the model cannot do from inside the pipeline is
get itself invoked, or tell the guardrails that a number is allowed to vanish.
Those two jobs are what lives here:

1. **Force the model to run.** :func:`~sway.cleanup.should_skip_cleanup`
   bypasses the LLM for short, clean transcripts -- and "meeting at 7, no, 8" is
   five words. Without a signal from this module the retracted time reaches the
   user's cursor verbatim.
2. **Exempt the retracted span from the entity guardrail.**
   :func:`~sway.cleanup.is_cleanup_output_safe` treats every number in the
   transcript as protected, so the *correct* rewrite ("...at 8am") looks exactly
   like the model having lost the "7", and is thrown away.
   :meth:`CorrectionScan.retracted_text` is what tells the guardrail which
   entities the speaker themselves withdrew.

Being wrong in either direction has a cost, and they are not symmetric. A missed
correction leaves the user with the literal transcript -- annoying, visible, and
fixable by hand. A false positive silently deletes something they meant to say.
So detection is deliberately conservative: an ambiguous marker ("no",
"actually", "sorry", "wait") only counts when the speech around it shows a
*comparable pair* -- a time replaced by a time, a name by a name -- or restates
a noun. Only the unmistakable phrases ("scratch that", "I mean", "make that")
fire on their own.

Pure functions over strings, per CLAUDE.md's rule to test the
transcript-processing functions rather than the audio path. No I/O, no network,
no model call; the whole scan costs microseconds and runs on every utterance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: How far either side of a marker we look for the entity it replaced. Wide
#: enough to cross a sentence boundary ("...Monday at 3pm. No, Tuesday at 3pm"),
#: narrow enough that two genuinely separate statements a paragraph apart are
#: never paired up.
_CONTEXT_WORDS = 12

#: Detection strictness. ``conservative`` is the default and the shipped
#: behaviour; ``balanced`` additionally accepts a bare ambiguous marker that has
#: content after it, which catches more real corrections at the cost of
#: occasionally trimming a sincere aside.
MODES = ("off", "conservative", "balanced")
DEFAULT_MODE = "conservative"

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

#: Phrases that mean "disregard what I just said" and cannot mean anything
#: else. These fire without needing corroborating context.
#:
#: Bare "rather" is deliberately absent, matching
#: ``sway.cleanup._SELF_CORRECTION_PATTERN``: "Rather than that, use the other
#: one" is ordinary speech. Only "or rather" is unambiguous.
_EXPLICIT_MARKERS = (
    "let me rephrase",
    "no scratch that",
    "scratch that",
    "strike that",
    "i take that back",
    "correction",
    "no wait",
    "wait no",
    "actually no",
    "no actually",
    "or rather",
    "make that",
    "i meant",
    "i mean",
)

#: Words that are *sometimes* a retraction and sometimes ordinary speech. These
#: need a comparable pair or a restatement before they count. Longest first, so
#: "no no" is never matched as a bare "no".
_AMBIGUOUS_MARKERS = (
    "no no no",
    "no no",
    # "make it" is the sibling of the explicit "make that", and deliberately
    # not in that tier. "Make that Tuesday" can only be a retraction; "make it
    # three copies" and "make it snappy" are ordinary requests, and treating
    # them as one would delete the clause before them. Here it must clear the
    # same bar as "actually" -- a comparable pair (a day replaced by a day, a
    # time by a time) or a restated noun -- which "no, no, make it Tuesday at
    # 6 AM" does and a plain instruction does not.
    "make it",
    "sorry",
    "actually",
    "wait",
    "no",
)


def _alternation(markers: tuple[str, ...]) -> str:
    """Regex alternation over marker phrases, tolerant of extra whitespace.

    Whisper occasionally renders "I mean" with a line break or a doubled space,
    so each literal space becomes ``\\s+`` rather than a single character.
    """
    return "|".join(re.escape(m).replace(r"\ ", r"\s+") for m in markers)


#: Anchored to a comma, dash, or sentence boundary on the left, exactly as
#: cleanup's own self-correction pattern is, so a sincere "I'm sorry about
#: that" or "I actually finished it" is never read as a retraction.
_ANCHOR = r"(?:^|[,;:—–-]\s*|\.{1,3}\s+)"

_EXPLICIT_RE = re.compile(
    _ANCHOR + r"(?P<marker>" + _alternation(_EXPLICIT_MARKERS) + r")\b",
    re.IGNORECASE,
)
_AMBIGUOUS_RE = re.compile(
    _ANCHOR + r"(?P<marker>" + _alternation(_AMBIGUOUS_MARKERS) + r")\b",
    re.IGNORECASE,
)

#: A coordinating conjunction immediately before an ambiguous marker means the
#: speaker is *adding* to what they said, not replacing it: "we shipped Monday
#: and, actually, Tuesday as well". Without this guard the two weekdays read as
#: a comparable pair and the first one would be thrown away.
_ADDITIVE_BEFORE_RE = re.compile(r"\b(?:and|or|plus|then|also)\s*[,;:\s]*$", re.IGNORECASE)
_ADDITIVE_AFTER_RE = re.compile(r"\b(?:as\s+well|too|also|in\s+addition|on\s+top)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------

#: A word, for entity classification. Hyphens are deliberately *excluded*:
#: Whisper renders a spoken dash with no surrounding spaces ("Friday-sorry"),
#: and folding that into one token hid the weekday from the classifier. A
#: hyphenated compound splitting into two words costs nothing here.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z’']*")

_WEEKDAYS = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday "
    "mon tue tues wed thu thur thurs fri sat sun "
    "today tomorrow yesterday".split()
)
_MONTHS = frozenset(
    "january february march april may june july august september october "
    "november december jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
_NUMBER_WORDS = frozenset(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty "
    "thirty forty fifty sixty seventy eighty ninety hundred thousand million "
    "billion".split()
)

#: Capitalised words that carry no identity: sentence starters, the pronoun
#: "I", and the marker words themselves. Without this, "Paris, no, London"
#: would read the "no" as a name and pair it with something.
_NOT_PROPER = frozenset(
    "i a an the no not yes ok okay so and but or if then "
    "sorry actually wait well hey oh um uh".split()
)

_TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|o'clock)\b|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
_DIGITS_RE = re.compile(r"\b\d[\d.,:/]*\b")

#: Entity classes. More specific spans are claimed first, so "7am" is a time
#: rather than a bare number and is never paired against "the 7 items"
#: elsewhere in the same sentence.
_CLASS_TIME = "time"
_CLASS_NUMBER = "number"
_CLASS_WEEKDAY = "weekday"
_CLASS_MONTH = "month"
_CLASS_PROPER = "proper"


@dataclass(frozen=True, slots=True)
class _Entity:
    kind: str
    value: str
    start: int
    end: int


def _entities(text: str) -> list[_Entity]:
    """Every classified entity in ``text``, left to right, non-overlapping."""
    found: list[_Entity] = []
    claimed: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(start < c_end and end > c_start for c_start, c_end in claimed)

    for match in _TIME_RE.finditer(text):
        claimed.append(match.span())
        found.append(_Entity(_CLASS_TIME, match.group(0).lower(), match.start(), match.end()))

    for match in _DIGITS_RE.finditer(text):
        if free(*match.span()):
            claimed.append(match.span())
            found.append(_Entity(_CLASS_NUMBER, match.group(0), match.start(), match.end()))

    for match in _WORD_RE.finditer(text):
        if not free(*match.span()):
            continue
        raw = match.group(0)
        lowered = raw.lower()
        if lowered in _WEEKDAYS:
            kind = _CLASS_WEEKDAY
        elif lowered in _MONTHS:
            kind = _CLASS_MONTH
        elif lowered in _NUMBER_WORDS:
            kind = _CLASS_NUMBER
        elif (
            raw[0].isupper()
            and lowered not in _NOT_PROPER
            and not _starts_sentence(text, match.start())
        ):
            kind = _CLASS_PROPER
        else:
            continue
        claimed.append(match.span())
        found.append(_Entity(kind, lowered, match.start(), match.end()))

    found.sort(key=lambda e: e.start)
    return found


def _starts_sentence(text: str, index: int) -> bool:
    """Whether the word at ``index`` opens the text or follows a full stop.

    Capitalisation only signals a proper noun when it is not simply the first
    word of a sentence -- Whisper capitalises those, and treating them as names
    would pair "Create a meeting..." with any other capitalised word nearby.
    """
    for char in reversed(text[:index]):
        if char.isspace():
            continue
        return char in ".!?…"
    return True


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

#: What kind of evidence fired. Recorded per marker so the latency annotation
#: and the eval harness can tell a strong signal from a weak one.
KIND_ENTITY = "entity"
KIND_RESTATEMENT = "restatement"
KIND_EXPLICIT = "explicit"
KIND_BARE = "bare"


@dataclass(frozen=True, slots=True)
class CorrectionMarker:
    """One retraction found in the transcript, and what it appears to withdraw."""

    marker: str
    #: Offsets of the marker phrase itself, into the original transcript.
    start: int
    end: int
    #: Offsets of the span the speaker withdrew: from the superseded entity (or
    #: the start of the abandoned clause) through the end of the marker.
    #: ``None`` when nothing identifiable was retracted.
    retracted: tuple[int, int] | None
    kind: str
    #: The entity class that was replaced, for :data:`KIND_ENTITY` markers.
    entity_class: str = ""


@dataclass(frozen=True, slots=True)
class CorrectionScan:
    """The outcome of scanning one transcript for self-corrections."""

    text: str
    markers: tuple[CorrectionMarker, ...] = ()

    @property
    def detected(self) -> bool:
        return bool(self.markers)

    @property
    def kinds(self) -> tuple[str, ...]:
        """The distinct evidence kinds that fired, in first-seen order."""
        return tuple(dict.fromkeys(m.kind for m in self.markers))

    def retracted_text(self) -> str:
        """The withdrawn spans, joined.

        Handed to :func:`sway.cleanup.is_cleanup_output_safe` so the entities
        inside them are exempt from the "the model lost a number" check. A
        rewrite may drop what the speaker themselves took back -- and only that.
        """
        parts = [self.text[m.retracted[0] : m.retracted[1]] for m in self.markers if m.retracted]
        return " ".join(part for part in parts if part.strip())

    def describe(self) -> str:
        """A short label for the latency annotation and the log line."""
        return ",".join(f"{m.marker}:{m.kind}" for m in self.markers)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan(text: str, *, mode: str = DEFAULT_MODE) -> CorrectionScan:
    """Find the self-corrections in ``text``.

    Returns an empty scan -- ``detected`` false, no markers -- for the
    overwhelmingly common case of speech that corrects nothing, at the cost of
    two regex passes over a short string.

    ``mode`` is one of :data:`MODES`. ``"off"`` disables detection entirely and
    is what the user's "automatic self-correction" switch turns off. An
    unrecognised mode is treated as :data:`DEFAULT_MODE` rather than raising,
    since it can only reach here from a hand-edited settings file.
    """
    if mode == "off" or not text.strip():
        return CorrectionScan(text=text)

    markers: list[CorrectionMarker] = []
    taken: list[tuple[int, int]] = []

    for match in _EXPLICIT_RE.finditer(text):
        span = match.span("marker")
        taken.append(span)
        markers.append(_explicit_marker(text, match.group("marker"), span))

    for match in _AMBIGUOUS_RE.finditer(text):
        span = match.span("marker")
        # An ambiguous marker sitting inside an explicit one ("no" within "no
        # wait", "actually" within "actually no") is already accounted for.
        if any(span[0] < end and span[1] > start for start, end in taken):
            continue
        found = _ambiguous_marker(text, match.group("marker"), span, mode=mode)
        if found is not None:
            taken.append(span)
            markers.append(found)

    if not markers:
        return CorrectionScan(text=text)
    markers.sort(key=lambda m: m.start)
    return CorrectionScan(text=text, markers=tuple(markers))


def _explicit_marker(text: str, marker: str, span: tuple[int, int]) -> CorrectionMarker:
    """An unmistakable retraction. It fires on its own; the only question left
    is what it withdrew."""
    before_start, before = _window_before(text, span[0])
    after = _window_after(text, span[1])
    pair = _comparable_pair(before, after)
    if pair is not None:
        left, _right = pair
        return CorrectionMarker(
            marker=_normalise(marker),
            start=span[0],
            end=span[1],
            retracted=(before_start + left.start, span[1]),
            kind=KIND_ENTITY,
            entity_class=left.kind,
        )
    # No comparable entity: the speaker withdrew a clause rather than a value.
    # Retract back to the clause boundary, not to the start of the utterance --
    # exempting more than they actually took back would weaken the guardrail
    # over the rest of the sentence.
    return CorrectionMarker(
        marker=_normalise(marker),
        start=span[0],
        end=span[1],
        retracted=(_clause_start(text, span[0]), span[1]),
        kind=KIND_EXPLICIT,
    )


def _ambiguous_marker(
    text: str, marker: str, span: tuple[int, int], *, mode: str
) -> CorrectionMarker | None:
    """A marker that is only sometimes a retraction. ``None`` means it is not
    one here, and the words around it must be left exactly as spoken."""
    before_start, before = _window_before(text, span[0])
    after = _window_after(text, span[1])

    # "we shipped Monday and, actually, Tuesday as well" -- adding, not
    # replacing. Checked first, because the two weekdays would otherwise look
    # exactly like a correction.
    if _ADDITIVE_BEFORE_RE.search(before) or _ADDITIVE_AFTER_RE.search(after):
        return None
    if not _has_content(after):
        # "I don't want to go, actually." Nothing but punctuation follows, so
        # nothing replaced anything: a sentence-final hedge, not a retraction.
        return None

    pair = _comparable_pair(before, after)
    if pair is not None:
        left, _right = pair
        return CorrectionMarker(
            marker=_normalise(marker),
            start=span[0],
            end=span[1],
            retracted=(before_start + left.start, span[1]),
            kind=KIND_ENTITY,
            entity_class=left.kind,
        )

    restated = _restated_noun(before, after)
    if restated is not None:
        return CorrectionMarker(
            marker=_normalise(marker),
            start=span[0],
            end=span[1],
            retracted=(before_start + restated, span[1]),
            kind=KIND_RESTATEMENT,
        )

    if mode == "balanced" and before.strip():
        return CorrectionMarker(
            marker=_normalise(marker),
            start=span[0],
            end=span[1],
            retracted=(_clause_start(text, span[0]), span[1]),
            kind=KIND_BARE,
        )
    return None


def _normalise(marker: str) -> str:
    return " ".join(marker.lower().split())


def _has_content(text: str) -> bool:
    """Whether ``text`` holds anything a speaker actually said.

    A trailing "." is not content -- that is what separates "I don't want to
    go, actually." from a real retraction. Digits count as much as letters:
    testing for a *word* alone silently broke "meeting at 7, no, 8", where
    everything after the marker is a number.
    """
    return _WORD_RE.search(text) is not None or _DIGITS_RE.search(text) is not None


def _window_before(text: str, index: int) -> tuple[int, str]:
    """The last :data:`_CONTEXT_WORDS` words before ``index``.

    Returns the window's offset into ``text`` alongside it, so an entity found
    inside can be reported at its position in the original transcript.
    """
    head = text[:index]
    words = list(_WORD_RE.finditer(head))
    if not words:
        return index, ""
    start = words[max(0, len(words) - _CONTEXT_WORDS)].start()
    return start, head[start:]


def _window_after(text: str, index: int) -> str:
    tail = text[index:]
    words = list(_WORD_RE.finditer(tail))
    if len(words) <= _CONTEXT_WORDS:
        return tail
    return tail[: words[_CONTEXT_WORDS].start()]


def _clause_start(text: str, index: int) -> int:
    """Where the clause containing ``index`` begins.

    Used as the retraction boundary when no specific entity was replaced.
    """
    for position in range(index - 1, -1, -1):
        if text[position] in ".!?;—–":
            return position + 1
    return 0


def _comparable_pair(before: str, after: str) -> tuple[_Entity, _Entity] | None:
    """The superseded entity and its replacement, if the two windows hold a
    matching pair.

    "Tuesday ... Wednesday" and "7am ... 8am" are corrections; "Tuesday ...
    3pm" is a speaker carrying on. The *last* matching entity before the marker
    is the one taken back, since that is the one they had just said.
    """
    left = _entities(before)
    right = _entities(after)
    if not left or not right:
        return None

    right_classes = {entity.kind for entity in right}
    for candidate in reversed(left):
        if candidate.kind not in right_classes:
            continue
        replacement = next(
            (e for e in right if e.kind == candidate.kind and e.value != candidate.value), None
        )
        if replacement is None:
            # "...at 3pm. No, Tuesday at 3pm" -- the repeated 3pm is carried
            # over, not corrected. Keep looking for the value that changed.
            continue
        return candidate, replacement
    return None


#: Content words that carry no identity, so repeating one across a marker is
#: not evidence of a restatement.
_FUNCTION_WORDS = frozenset(
    "the a an is are was were be been being to of in on at for with and or but "
    "it its this that these those we i you he she they them us our your my his "
    "her their do does did done have has had will would can could should shall "
    "not no so if then than as by from up out about into over after before "
    "sorry actually wait mean meant make send".split()
)


def _restated_noun(before: str, after: str) -> int | None:
    """Start offset (within ``before``) of a noun the speaker repeated across
    the marker.

    "the old API -- sorry, the new API" replaces a modifier rather than an
    entity, so no comparable pair exists; the repeated "API" is what marks it as
    a restatement rather than a new thought. The repeated word has to carry
    meaning, which is what :data:`_FUNCTION_WORDS` filters out.
    """
    after_words = {
        match.group(0).lower()
        for match in _WORD_RE.finditer(after)
        if match.group(0).lower() not in _FUNCTION_WORDS
    }
    if not after_words:
        return None
    for match in reversed(list(_WORD_RE.finditer(before))):
        word = match.group(0).lower()
        if word in _FUNCTION_WORDS or len(word) < 2:
            continue
        if word in after_words:
            return _clause_start(before, match.start())
    return None


__all__ = [
    "DEFAULT_MODE",
    "KIND_BARE",
    "KIND_ENTITY",
    "KIND_EXPLICIT",
    "KIND_RESTATEMENT",
    "MODES",
    "CorrectionMarker",
    "CorrectionScan",
    "scan",
]
