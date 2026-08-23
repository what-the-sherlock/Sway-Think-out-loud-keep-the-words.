"""Zero-cost spoken triggers: a phrase that becomes text instead of staying
in the transcript.

This is the local, pre-LLM half of D8 and deliberately *only* that half. Two
action types are implemented, both resolved entirely in-process:

``macro``  the phrase is replaced by a stored block of text
``date``   the phrase is replaced by today's date

Both cost zero tokens and zero milliseconds of network time, which is the
whole reason they are safe to check against every utterance. D8's ``mode``
and ``transform`` types -- the ones that need a model call -- are not here;
adding them means adding a prompt-budget story, and that is D8 proper.

Everything in this module is a pure function over strings, per CLAUDE.md's
rule to test the transcript-processing functions rather than the audio path.
:func:`apply_triggers` is called on the transcript *before* cleanup, so a
substituted email address or signature is never rewritten by the LLM.

The two failure modes that matter here are the ones D8 names:

* **Silently eating words.** A trigger that fires by accident deletes what
  the user said. So matching is whole-phrase and word-boundary anchored --
  never a substring -- and the raw pre-substitution transcript is always
  returned alongside the result so history can preserve it.
* **Order-dependent surprises.** Longer phrases are matched first, so a
  trigger "my email address" is never pre-empted by "my email".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sway.settings import normalise_phrase

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sway.settings import Trigger

#: Characters stripped from the edges of a candidate span before comparison.
#: Whisper punctuates, so "my email." must match the trigger "my email".
_EDGE = " \t\r\n.,!?;:'\"«»…“”‘’"

#: Splits a transcript into words plus the whitespace between them, so a
#: substitution can be spliced back in without disturbing the original
#: spacing of everything around it.
_TOKEN_RE = re.compile(r"\S+")

#: Spoken lead-ins that may precede a trigger phrase. "Use my intro email"
#: fires the "intro email" snippet, which is how a snippet gets invoked by
#: voice without the user having to name it bare mid-sentence.
#:
#: These make matching *stricter*, not looser: the bare phrase still fires on
#: its own exactly as before, and a prefixed match is simply longer, so
#: longest-first ordering means "use my email" is never resolved as the
#: shorter "email" with a stray "use my" left behind.
#:
#: Every entry is possessive and instructional. "use the" was tried and
#: dropped: it is ordinary English ("use the new endpoint"), and since the
#: bare phrase fires regardless, including it would only ever have deleted two
#: words the speaker meant to keep.
DEFAULT_PREFIXES: tuple[str, ...] = ("use my", "insert my", "paste my")


@dataclass(frozen=True, slots=True)
class TriggerHit:
    """One trigger that fired, and where."""

    trigger_id: str
    phrase: str
    replacement: str
    #: Character offsets into the *original* transcript.
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TriggerResult:
    """The outcome of resolving every trigger against one transcript."""

    #: The transcript with every fired trigger substituted.
    text: str
    #: What was transcribed, untouched. Preserved so a false positive is
    #: always recoverable from history -- D8.5's undo requirement.
    raw_text: str
    hits: tuple[TriggerHit, ...] = ()

    @property
    def fired(self) -> bool:
        return bool(self.hits)


def resolve_payload(trigger: Trigger, *, today: date | None = None) -> str:
    """The text a fired trigger inserts.

    ``date`` renders the current date rather than a stored string, which is
    the entire point of it being its own type: a macro would freeze the day
    it was created.
    """
    if trigger.type == "date":
        day = today or date.today()
        # Platform-independent: %-d/%#d differ between POSIX and Windows, so
        # the day is formatted by hand rather than by strftime.
        return f"{day.day} {day.strftime('%B %Y')}"
    return trigger.payload


def _tokenise(text: str) -> list[tuple[int, int, str]]:
    """(start, end, normalised_word) for every whitespace-delimited token.

    The offsets bound the *trimmed* word, not the raw token, so punctuation
    sitting against a trigger survives the substitution: "send it to my
    email." must become "send it to a@b.com." and keep its full stop, rather
    than having the sentence's punctuation eaten along with the phrase.
    """
    tokens: list[tuple[int, int, str]] = []
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0)
        stripped = raw.strip(_EDGE)
        if not stripped:
            continue
        lead = len(raw) - len(raw.lstrip(_EDGE))
        start = match.start() + lead
        tokens.append((start, start + len(stripped), stripped.lower()))
    return tokens


def _phrase_words(phrase: str) -> tuple[str, ...]:
    return tuple(normalise_phrase(phrase).split())


def find_conflicts(triggers: Sequence[Trigger]) -> dict[str, list[str]]:
    """Map trigger id -> ids of triggers that would pre-empt or shadow it.

    Two relationships count as a conflict, and they are different problems:

    * **Identical** phrases -- only the first can ever fire.
    * **Prefix containment** -- "my email" and "my email address" both match
      the same speech, and since longer phrases win, the shorter one still
      fires everywhere the longer one does not. That is usually intended, but
      the user should be told it is happening.

    Reported symmetrically so the UI can flag both rows rather than
    arbitrarily blaming one.
    """
    conflicts: dict[str, list[str]] = {}
    words = {t.id: _phrase_words(t.phrase) for t in triggers}

    for outer in triggers:
        a = words[outer.id]
        if not a:
            continue
        for inner in triggers:
            if inner.id == outer.id:
                continue
            b = words[inner.id]
            if not b:
                continue
            if a == b or _is_prefix(a, b) or _is_prefix(b, a):
                conflicts.setdefault(outer.id, []).append(inner.id)
    return conflicts


def _is_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def apply_triggers(
    text: str,
    triggers: Iterable[Trigger],
    *,
    today: date | None = None,
    prefixes: Sequence[str] = DEFAULT_PREFIXES,
) -> TriggerResult:
    """Substitute every enabled trigger found in ``text``.

    Matching rules, chosen so an accidental fire is as unlikely as possible:

    * **Word-aligned only.** A phrase matches a run of whole words. "my
      email" never fires inside "beautify my emails today".
    * **Longest first.** A three-word trigger beats an overlapping one-word
      trigger regardless of the order they were defined in.
    * **Non-overlapping.** Once a span is consumed, no other trigger may
      claim any part of it, so two triggers can never delete each other's
      words.
    * **Left to right**, so multiple distinct triggers in one utterance all
      fire.
    * **Lead-ins are matched too.** "Use my intro email" fires the "intro
      email" snippet and consumes the lead-in with it, so the words "use my"
      do not survive into the pasted text. A snippet's *name* also matches,
      but only behind a lead-in. Pass ``prefixes=()`` to require the bare
      phrase and nothing else.

    Returns the original text unchanged (and ``fired == False``) when nothing
    matches, which is the overwhelmingly common case and costs one pass over
    the tokens.
    """
    enabled = [t for t in triggers if t.enabled and _phrase_words(t.phrase)]
    if not enabled or not text.strip():
        return TriggerResult(text=text, raw_text=text)

    tokens = _tokenise(text)
    if not tokens:
        return TriggerResult(text=text, raw_text=text)

    # One candidate per (trigger, spoken form): the bare phrase, plus the same
    # phrase behind each lead-in. Built here rather than at save time so a
    # change to the prefix list takes effect without rewriting settings.json.
    candidates: list[tuple[Trigger, tuple[str, ...]]] = []
    for trigger in enabled:
        words = _phrase_words(trigger.phrase)
        candidates.append((trigger, words))
        leads = [_phrase_words(prefix) for prefix in prefixes]
        for lead in leads:
            if lead:
                candidates.append((trigger, lead + words))
        # The display name is matched too, but *only* behind a lead-in. A name
        # is a casual label ("Address", "Signature") where a phrase is chosen
        # to be unique, so matching a name bare would fire on ordinary speech.
        # "use my signature" cannot be anything but a request for the snippet.
        name_words = _phrase_words(trigger.name)
        if name_words and name_words != words:
            for lead in leads:
                if lead:
                    candidates.append((trigger, lead + name_words))

    # Longest phrase first; ties broken by phrase text so the outcome is
    # deterministic rather than dependent on dict/list ordering.
    ordered = sorted(candidates, key=lambda c: (-len(c[1]), " ".join(c[1])))

    consumed = [False] * len(tokens)
    hits: list[TriggerHit] = []

    for trigger, needle in ordered:
        span = len(needle)
        index = 0
        while index + span <= len(tokens):
            window = tokens[index : index + span]
            if any(consumed[index + offset] for offset in range(span)):
                index += 1
                continue
            if tuple(word for _, _, word in window) == needle:
                for offset in range(span):
                    consumed[index + offset] = True
                hits.append(
                    TriggerHit(
                        trigger_id=trigger.id,
                        phrase=trigger.phrase,
                        replacement=resolve_payload(trigger, today=today),
                        start=window[0][0],
                        end=window[-1][1],
                    )
                )
                index += span
            else:
                index += 1

    if not hits:
        return TriggerResult(text=text, raw_text=text)

    ordered_hits = tuple(sorted(hits, key=lambda hit: hit.start))
    rebuilt: list[str] = []
    cursor = 0
    for hit in ordered_hits:
        rebuilt.append(text[cursor : hit.start])
        rebuilt.append(hit.replacement)
        cursor = hit.end
    rebuilt.append(text[cursor:])

    return TriggerResult(
        text=_tidy(("".join(rebuilt))),
        raw_text=text,
        hits=ordered_hits,
    )


def _tidy(text: str) -> str:
    """Clean up the seams a substitution leaves behind.

    Replacing a phrase with an empty payload, or with text that already ends
    in punctuation, otherwise leaves doubled spaces or " ." artefacts that
    would reach the user's cursor.
    """
    collapsed = re.sub(r"[ \t]{2,}", " ", text)
    collapsed = re.sub(r"\s+([.,!?;:])", r"\1", collapsed)
    return collapsed.strip()


__all__ = [
    "DEFAULT_PREFIXES",
    "TriggerHit",
    "TriggerResult",
    "apply_triggers",
    "find_conflicts",
    "resolve_payload",
]
