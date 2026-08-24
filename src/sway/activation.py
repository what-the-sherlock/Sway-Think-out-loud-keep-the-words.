"""One user intention, one execution.

Every way of starting work in this app -- holding the dictation key, tapping a
chord, toggling hands-free, holding the command key -- ends up funnelled
through a press that sets something up and a release that dispatches a
pipeline. Nothing used to connect the two. A release simply read whatever
``_press_time`` and ``_start_frame`` happened to hold, and those were never
cleared, so a *second* release with no press in between re-dispatched the same
audio range: two transcriptions of one utterance, two model calls against a
free-tier quota, and two pastes landing back to back with no separator.

That is not hypothetical. It is in the shipped user's own data -- two history
rows 130 ms apart with identical text, and a matching pair of runs for every
activation after one particular app restart.

This module is the ledger that makes it impossible. A press *mints* a token; a
release *claims* it, and a token can only be claimed once; the injector *marks*
it, and a token can only paste once. Whatever produced the duplicate event --
a doubled OS hook, a rival process that outlived its tray icon, a retry, a
race between a toggle and its safety timeout -- the second one finds the token
already spent and does nothing.

Why a ledger rather than a lock around the pipeline: a lock would serialise the
duplicate, not prevent it. The user would still get two pastes, just not
interleaved. What has to be established is that the two events refer to the
*same intention*, and identity is what a token gives.

Pure: no threads of its own beyond the lock that guards its own state, no I/O,
no clock. Directly unit-testable, per CLAUDE.md.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

#: The independent activation paths. Each keeps its own outstanding slot, so a
#: command held while a dictation is in flight is not mistaken for a duplicate
#: of it -- they are genuinely two intentions.
DICTATION = "dictation"
COMMAND = "command"

#: How many spent tokens to remember for the injection check. Only needs to
#: outlive the window in which a duplicate can plausibly arrive (milliseconds
#: to seconds); a handful covers a rival process racing us, and the bound is
#: what stops a long-running session accumulating ids forever.
_REMEMBERED = 32


@dataclass(frozen=True, slots=True)
class Activation:
    """One user intention, from press to paste."""

    id: int
    source: str

    def __str__(self) -> str:  # pragma: no cover - logging sugar
        return f"{self.source}#{self.id}"


class ActivationLedger:
    """Mints activation tokens and enforces that each is used once.

    Thread-safe: presses and releases arrive on pynput listener threads, the
    dispatch runs on worker threads, and the safety-timeout pump is a third.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._outstanding: dict[str, Activation] = {}
        self._injected: list[int] = []

    def begin(self, source: str) -> Activation:
        """Mint a token for a press.

        A press while one is already outstanding *replaces* it and returns the
        new token. That is deliberate: the old intention can no longer be
        completed (its release never came, or was swallowed), and keeping it
        would let a stale release dispatch it later. The abandoned token is
        simply never claimed.
        """
        with self._lock:
            activation = Activation(id=self._next_id, source=source)
            self._next_id += 1
            self._outstanding[source] = activation
            return activation

    def claim(self, source: str) -> Activation | None:
        """Take the outstanding token for a release, or ``None``.

        ``None`` means this release has no press behind it -- either the press
        never happened, or another release already claimed it. Both are cases
        where dispatching would duplicate work, so the caller returns without
        doing anything.
        """
        with self._lock:
            return self._outstanding.pop(source, None)

    def once(self, source: str) -> Activation:
        """A token for a gesture that has no separate release.

        A shortcut chord fires once and dispatches immediately; there is no
        press/release pair to guard. Minting *and* retiring in one step keeps
        the outstanding slot clean -- a token left sitting there would be
        claimed later by an unrelated release of the real key -- while still
        producing an identity the injection guard can use.
        """
        with self._lock:
            activation = Activation(id=self._next_id, source=source)
            self._next_id += 1
            return activation

    def abandon(self, source: str) -> None:
        """Drop the outstanding token without dispatching it.

        For a press that turns out to produce nothing -- a hold too short to
        count, a session stopped before it started. Keeps a stale token from
        being claimed by an unrelated later release.
        """
        with self._lock:
            self._outstanding.pop(source, None)

    def outstanding(self, source: str) -> Activation | None:
        with self._lock:
            return self._outstanding.get(source)

    def mark_injected(self, activation: Activation) -> bool:
        """Claim the right to paste for this activation.

        Returns ``True`` for the first caller and ``False`` for every one
        after it. This is the last line of defence and the only one that holds
        against a *second process* with its own ledger -- there it cannot help,
        which is why the quit path and the single-instance guard are fixed too.
        Within one process it makes a retry, a re-entered pipeline, or a
        doubled dispatch unable to paste twice.
        """
        with self._lock:
            if activation.id in self._injected:
                return False
            self._injected.append(activation.id)
            del self._injected[:-_REMEMBERED]
            return True

    def has_injected(self, activation: Activation) -> bool:
        with self._lock:
            return activation.id in self._injected


__all__ = ["COMMAND", "DICTATION", "Activation", "ActivationLedger"]
