"""Tests for the one-intention-one-execution ledger.

Every case here corresponds to a way the shipped app was observed to run one
utterance twice, or to a way it plausibly could. The failure being guarded
against is expensive and invisible: two transcriptions of the same audio, two
model calls against a free-tier quota, and two pastes landing back to back
with no separator between them.
"""

from __future__ import annotations

import threading

from sway.activation import COMMAND, DICTATION, Activation, ActivationLedger


class TestClaiming:
    def test_a_press_and_release_pair_dispatches_once(self) -> None:
        ledger = ActivationLedger()
        minted = ledger.begin(DICTATION)
        assert ledger.claim(DICTATION) == minted

    def test_a_second_release_finds_nothing(self) -> None:
        """The reported duplicate: one hold, two releases, two pastes. The
        second release used to re-read the same `_start_frame`, because it was
        never cleared."""
        ledger = ActivationLedger()
        ledger.begin(DICTATION)
        assert ledger.claim(DICTATION) is not None
        assert ledger.claim(DICTATION) is None

    def test_a_release_with_no_press_finds_nothing(self) -> None:
        assert ActivationLedger().claim(DICTATION) is None

    def test_dictation_and_command_are_independent_intentions(self) -> None:
        """A command held while a dictation is in flight is not a duplicate of
        it, so neither may consume the other's token."""
        ledger = ActivationLedger()
        dictation = ledger.begin(DICTATION)
        command = ledger.begin(COMMAND)
        assert ledger.claim(COMMAND) == command
        assert ledger.claim(DICTATION) == dictation

    def test_a_second_press_supersedes_an_unclaimed_one(self) -> None:
        """A press whose release never arrived must not leave a token a later,
        unrelated release can pick up."""
        ledger = ActivationLedger()
        first = ledger.begin(DICTATION)
        second = ledger.begin(DICTATION)
        assert first != second
        assert ledger.claim(DICTATION) == second
        assert ledger.claim(DICTATION) is None

    def test_abandon_retires_a_token_without_dispatching(self) -> None:
        ledger = ActivationLedger()
        ledger.begin(DICTATION)
        ledger.abandon(DICTATION)
        assert ledger.claim(DICTATION) is None

    def test_once_leaves_nothing_outstanding(self) -> None:
        """A shortcut chord fires and dispatches in one step. A token left
        outstanding would be claimed later by a release of the real key."""
        ledger = ActivationLedger()
        activation = ledger.once(COMMAND)
        assert ledger.outstanding(COMMAND) is None
        assert activation.id > 0

    def test_every_token_is_distinct(self) -> None:
        ledger = ActivationLedger()
        ids = {ledger.once(DICTATION).id for _ in range(50)}
        assert len(ids) == 50


class TestInjectionGuard:
    def test_only_the_first_holder_may_paste(self) -> None:
        """The last line of defence: whatever re-entered the pipeline -- a
        retry, a duplicate dispatch -- only one paste reaches the cursor."""
        ledger = ActivationLedger()
        activation = ledger.begin(DICTATION)
        assert ledger.mark_injected(activation) is True
        assert ledger.mark_injected(activation) is False
        assert ledger.mark_injected(activation) is False

    def test_a_retry_of_the_same_activation_cannot_paste_again(self) -> None:
        ledger = ActivationLedger()
        activation = ledger.begin(DICTATION)
        ledger.mark_injected(activation)
        assert ledger.has_injected(activation)

    def test_separate_activations_each_paste_once(self) -> None:
        ledger = ActivationLedger()
        first = ledger.begin(DICTATION)
        ledger.claim(DICTATION)
        second = ledger.begin(DICTATION)
        ledger.claim(DICTATION)
        assert ledger.mark_injected(first) is True
        assert ledger.mark_injected(second) is True

    def test_the_spent_list_stays_bounded(self) -> None:
        """A long-running session must not accumulate ids forever."""
        ledger = ActivationLedger()
        for _ in range(500):
            ledger.mark_injected(ledger.once(DICTATION))
        assert len(ledger._injected) <= 32


class TestConcurrency:
    def test_exactly_one_of_many_racing_releases_wins(self) -> None:
        """The toggle-versus-safety-timeout race: two threads decide to stop
        the same session in the same instant."""
        ledger = ActivationLedger()
        ledger.begin(DICTATION)
        claimed: list[Activation] = []
        barrier = threading.Barrier(8)

        def claim() -> None:
            barrier.wait()
            got = ledger.claim(DICTATION)
            if got is not None:
                claimed.append(got)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(claimed) == 1

    def test_exactly_one_of_many_racing_injections_wins(self) -> None:
        ledger = ActivationLedger()
        activation = ledger.begin(DICTATION)
        wins: list[bool] = []
        barrier = threading.Barrier(8)

        def inject() -> None:
            barrier.wait()
            wins.append(ledger.mark_injected(activation))

        threads = [threading.Thread(target=inject) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert wins.count(True) == 1
