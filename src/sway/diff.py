"""A word-level diff between two pieces of text.

Used by the transform preview panel (a visible before/after while nothing has
been pasted yet) and by the dashboard editor's stored "example of transformed
text". ``difflib`` is stdlib and already exactly the right tool -- no need for
anything else, per CLAUDE.md's "prefer stdlib, minimal dependencies" rule.

Pure: no I/O, no Tk, no network. Directly unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

#: A "word" for diffing purposes is a run of non-whitespace, or a run of
#: whitespace. Splitting out whitespace as its own token (rather than
#: dropping it) means the diff can be rejoined back into readable text
#: exactly, including the original spacing.
_TOKEN_RE = re.compile(r"\s+|\S+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


@dataclass(frozen=True, slots=True)
class DiffPart:
    """One span of the diff: unchanged, removed (from ``before``), or added
    (from ``after``)."""

    op: str  # "equal" | "delete" | "insert"
    text: str


def word_diff(before: str, after: str) -> list[DiffPart]:
    """A word-level diff between ``before`` and ``after``.

    Adjacent tokens of the same operation are merged into one part, so a
    five-word unchanged run is one ``DiffPart`` rather than five -- what a
    renderer actually wants to iterate over.
    """
    before_tokens = _tokenize(before)
    after_tokens = _tokenize(after)
    matcher = SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)

    parts: list[DiffPart] = []
    for tag, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if tag == "equal":
            parts.append(DiffPart("equal", "".join(before_tokens[a_start:a_end])))
        elif tag == "delete":
            parts.append(DiffPart("delete", "".join(before_tokens[a_start:a_end])))
        elif tag == "insert":
            parts.append(DiffPart("insert", "".join(after_tokens[b_start:b_end])))
        elif tag == "replace":
            parts.append(DiffPart("delete", "".join(before_tokens[a_start:a_end])))
            parts.append(DiffPart("insert", "".join(after_tokens[b_start:b_end])))
    return parts


def change_count(parts: list[DiffPart]) -> int:
    """How many non-equal parts the diff has -- what a panel titled "N
    Changes" actually counts. A delete+insert pair from one "replace" opcode
    counts as two, matching how a reader sees it: one thing removed, one
    thing added, not "one change"."""
    return sum(1 for p in parts if p.op != "equal")


def as_dicts(parts: list[DiffPart]) -> list[dict[str, str]]:
    """JSON-shaped form, for handing a diff to the dashboard or the bar."""
    return [{"op": p.op, "text": p.text} for p in parts]


__all__ = ["DiffPart", "as_dicts", "change_count", "word_diff"]
