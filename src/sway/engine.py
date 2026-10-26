"""Wires capture, VAD, FLAC encoding, Groq transcription, LLM cleanup, and
paste injection into the dictation pipeline, with every stage boundary
measured by the Phase 0 :class:`~flow.latency.tracker.LatencyTracker`.

Threading model (see CLAUDE.md's architecture rules for why):

* :class:`~flow.audio.capture.AudioCapture` opens the mic once at
  :meth:`Engine.start` and its callback only writes into the ring buffer.
* :class:`~flow.hotkey.BindingListener` runs one pynput keyboard hook and one
  mouse hook for every binding at once: dictation's, the command's,
  hands-free's, and every transform's, however many each holds. A
  hold-shaped binding's "disengage" hands its recorded span off to a fresh
  worker thread rather than processing inline, so the listener is free to
  see the next event immediately.
* :class:`~flow.streaming.ChunkStreamer` (Phase 4) runs its own background
  thread, started on press and stopped on release, that ships overlapping
  audio chunks to Groq while the user is still speaking. See
  :meth:`_transcribe` for how its results are merged, or discarded in favor
  of a single whole-clip request if anything about it failed.
* :class:`~flow.net_loop.NetworkLoop` runs one asyncio loop in one thread for
  every Groq request.
* :class:`~flow.overlay.Overlay` runs Tk's mainloop on whichever thread calls
  :meth:`Engine.run_overlay` (the main thread, by convention).
  :meth:`~flow.overlay.Overlay.run_on_ui_thread` lets the tray icon's own
  thread (Phase 5) safely open :class:`~flow.history_window.HistoryWindow` on
  that same Tk thread.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sway import APP_NAME, instance
from sway.audio.capture import AudioCapture, input_devices
from sway.analytics import SessionMetrics, count_words, pcm_duration_ms, session_metrics

from sway.activation import COMMAND, DICTATION, Activation, ActivationLedger
from sway.audio.flac import encode_flac
from sway.audio.ring_buffer import RingBuffer
from sway.audio.vad import VadTrimmer
from sway.cleanup import (
    NO_LANGUAGE_INTENT,
    SPOKEN_COMMAND_EXPECTATION,
    CleanupStats,
    GuardrailMode,
    LanguageIntent,
    OutputExpectation,
    advisory_note,
    apply_guardrail,
    language_intent_for,
    looks_hallucinated,
    normalize_cleanup_output,
    parse_language_header,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from sway.commands import COMMAND_SYSTEM_PROMPT, build_command_user_message
from sway import languages
from sway.config import Config
from sway.config import _validate  # noqa: PLC2701 - deliberately re-validating a settings-merged config
from sway.corrections import CorrectionScan
from sway.corrections import scan as scan_corrections
from sway.dashboard.server import DashboardServer, EngineControl
from sway.dictionary import (
    DictionaryStore,
    build_prompt_bias,
    candidate_terms,
    prioritise,
)
from sway.errors import (
    AudioError,
    ClipboardError,
    ConfigError,
    DictionaryStoreError,
    SwayError,
    HistoryStoreError,
    PasteBlockedError,
)
from sway.groq_client import (
    WHISPER_MODEL,
    GroqClient,
    GroqError,
    GroqRateLimitError,
    TranscriptionResult,
)
from sway.history import KIND_COMMAND, KIND_DICTATION, HistoryStore
from sway.history_window import HistoryWindow
from sway.hotkey import (
    Binding,
    BindingError,
    BindingListener,
    DoubleTapTracker,
    RefCountedGate,
    TapModeTracker,
    effective_bindings,
    parse_binding,
    prettify_hotkey,
)
from sway.inject import TextInjector
from sway.latency.models import Stage
from sway.latency.tracker import LatencyTracker, RunRecorder
from sway.net_loop import NetworkLoop
from sway.bar import (
    ACTION_LANGUAGE,
    ACTION_MIC,
    BarState,
    MenuItem,
    NullBar,
    SwayBar,
    TransformReviewPanel,
)
from sway.diff import change_count, word_diff
from sway.platform import PlatformAdapter, get_platform
from sway.profiles import CleanupStyle, style_for_process
from sway.selection import SelectionReader, selection_budget_s
from sway.settings import SettingsStore, apply_to_config
from sway import transforms as transforms_module
from sway.transforms import Transform, build_directive_block, expectation_for
from sway.triggers import apply_triggers
from sway.streaming import ChunkSessionResult, ChunkStreamer, max_overlap_words, stitch_transcripts
from sway.tray import TrayIcon, TrayState
from sway.window import AppWindow

logger = logging.getLogger(__name__)


def _mentions(text: str, term: str) -> bool:
    """Whether ``term`` appears in ``text`` as whole words.

    Case-insensitive, because the user typed "Kubernetes" and Whisper may well
    have written "kubernetes". Word-bounded, so "API" is not counted as used
    every time someone says "capability".
    """
    if not term.strip():
        return False
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


def should_process(held_ms: float, min_hold_ms: int) -> bool:
    """Pure guard: was the hold long enough to be a deliberate dictation?"""
    return held_ms >= min_hold_ms


def _drop_duplicate_bindings(
    mapping: dict[str, tuple], *, kind: str
) -> tuple[dict[str, tuple], dict[str, str]]:
    """Keep the first registration of each distinct :class:`~sway.hotkey.Binding`
    value, dropping any later one that repeats it.

    Two different actions accidentally bound to the exact same physical
    gesture is genuinely broken -- BindingListener would otherwise register
    both, and only one could ever actually claim a given press. The save
    paths (``save_activation_binding``, ``save_transform``) already refuse
    an activation-target collision outright and flag a transform collision
    as a visible warning, so this should rarely trigger in practice; it
    exists as the same defensive backstop :meth:`_bind_transform_shortcuts`
    (the module this replaced) always had, now covering hold-shaped
    bindings too. Insertion order is registration priority: dictation, then
    command, then hands-free, then transforms, in the order
    :meth:`Engine._rebind_all_bindings` registers them.

    Returns ``(kept, dropped)`` -- ``dropped`` maps a losing slot id to the
    slot id that won, so the registration ledger (see
    :meth:`Engine._rebind_all_bindings`) can say not just *that* a binding
    was dropped but *which* one it lost to.
    """
    seen: dict[Any, str] = {}
    result: dict[str, tuple] = {}
    dropped: dict[str, str] = {}
    for slot_id, entry in mapping.items():
        binding = entry[0]
        if binding in seen:
            winner = seen[binding]
            logger.warning("%s %s is bound twice; keeping %s", kind, binding, winner)
            dropped[slot_id] = winner
            continue
        seen[binding] = slot_id
        result[slot_id] = entry
    return result, dropped


def preroll_start_frame(mark: int, preroll_frames: int) -> int:
    """Where a dictation should actually start reading from the ring buffer.

    People start speaking *as* they press the key, not strictly after it, and
    a global hotkey adds its own few tens of milliseconds on top. The mic
    stream is always open and always writing (CLAUDE.md), so the audio from
    just before the press is already sitting in the ring buffer and costs
    nothing to include -- the VAD trims it back off when the lead-in really
    was silent. Clamped at 0 for the first moments after startup, when fewer
    frames exist than the pre-roll asks for.
    """
    return max(0, mark - preroll_frames)


def frames_before(mark: int, seconds: float, sample_rate: int) -> int:
    """The ring position ``seconds`` before ``mark``, clamped at 0.

    A dictation engage can be deferred by ``sway.hotkey``'s prefix grace (up
    to PREFIX_GRACE_S) so a chord sharing the same bare hold key can be told
    apart first -- see BindingListener._begin_hold_locked. The mic stream
    has been writing throughout regardless (CLAUDE.md), so that grace window
    is already sitting in the ring buffer and costs nothing to reclaim: this
    is what makes the deferral free rather than a hole at the front of the
    dictation. ``seconds`` is negative-clamped to 0 so a caller passing
    ``now - press_time`` for an *undeferred* press (where the two are equal)
    is a no-op, not a forward seek.
    """
    return max(0, mark - int(max(0.0, seconds) * sample_rate))


@dataclass(frozen=True, slots=True)
class BindingRecord:
    """One configured binding's registration status, for the dashboard's
    read-only "Registered shortcuts" table (see :meth:`Engine.describe`) and
    for the one-line-per-binding log :meth:`Engine._rebind_all_bindings`
    writes on every rebind.

    Built in the engine, not in :class:`~sway.hotkey.BindingListener`: the
    listener only ever sees already-parsed :class:`~sway.hotkey.Binding`
    objects, with no idea of raw strings, parse failures, or which action a
    slot id belongs to -- ``_rebind_all_bindings`` is the one place that
    sees all of that at once.
    """

    slot_id: str  # e.g. "dictation:0", "transform:<id>:1"
    owner: str  # "dictation" | "command" | "hands_free" | "transform:<id>"
    label: str  # human name, e.g. 'the "Turn into an email" transform'
    value: str  # the raw stored binding string
    pretty: str  # Binding.pretty(), or "" when it did not parse
    shape: str  # "hold" | "tap" | "" (empty when it did not parse)
    status: str  # "registered" | "invalid" | "duplicate"
    detail: str  # the BindingError text, or the slot id that won, or ""


def _failure_label(exc: Exception) -> str:
    """A short, stable tag for a stage failure, for the latency record."""
    if isinstance(exc, GroqRateLimitError):
        return f"rate_limited:{exc.limit_type or 'unknown'}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


def _describe_failure(exc: Exception) -> dict[str, Any]:
    """Turn an exception into the message Home shows.

    The split that matters is **actionable or not**, not severity: red is
    reserved for a condition only the user can clear (docs/ui-derived-screens.md
    §2.1). A network blip and a revoked key are both "errors"; only one of
    them is worth colouring.
    """
    if isinstance(exc, PasteBlockedError):
        return {
            "kind": "paste_blocked",
            "headline": "Couldn't paste into that window",
            # Not a loss: the injector still put the text on the clipboard.
            "detail": "Your text is on the clipboard — press Ctrl+V to place it.",
            "actionable": False,
        }
    if isinstance(exc, ClipboardError):
        return {
            "kind": "clipboard",
            "headline": "The clipboard was unavailable",
            "detail": "Another app was holding it. Try that dictation again.",
            "actionable": False,
        }
    if isinstance(exc, AudioError):
        return {
            "kind": "audio",
            "headline": "Sway can't reach your microphone",
            "detail": "Another app may have taken exclusive control of it.",
            "actionable": True,
        }
    if isinstance(exc, GroqError):
        text = str(exc)
        if "rejected this API key" in text or "401" in text:
            return {
                "kind": "auth",
                "headline": "Groq rejected your API key",
                "detail": "It may have been revoked. Add a new key in Settings.",
                "actionable": True,
            }
        return {
            "kind": "network",
            "headline": "Couldn't reach Groq",
            "detail": "Retrying. Check your connection if it keeps happening.",
            "actionable": False,
        }
    return {
        "kind": "failed",
        "headline": "That dictation failed",
        "detail": str(exc),
        "actionable": False,
    }


#: How many microphones the bar's popover lists before deferring to Settings.
#: A picker is a shortcut; a machine with a dozen inputs should not get a
#: panel taller than the window it is floating over.
_MIC_POPOVER_ROWS = 6


def _short_failure(exc: Exception) -> str:
    """A pill-sized label. The pill is ~34 characters wide at most."""
    if isinstance(exc, PasteBlockedError):
        return "copied instead"
    if isinstance(exc, AudioError):
        return "no microphone"
    if isinstance(exc, GroqError) and "rejected this API key" in str(exc):
        return "key rejected"
    if isinstance(exc, GroqError):
        return "no connection"
    return "failed"


#: How many recent language verdicts to keep. Three is enough to ride out a
#: single misdetection without making a genuine language switch feel sticky.
_LANGUAGE_HISTORY = 3

#: Below this much speech, Whisper's own language label is discarded: measured
#: misdetections on this pipeline are concentrated under roughly two seconds
#: ("Hello" -> hindi, "Mayfair." -> korean, "Hallo" -> dutch).
_LANGUAGE_MIN_SPEECH_MS = 2000.0

#: A re-transcribe on the accurate model is only worth its round trip while
#: the clip is short; past this, the retry costs more than the error.
_STT_RETRY_MAX_SPEECH_MS = 4000.0


@dataclass(frozen=True, slots=True)
class _Transcript:
    """A raw transcript plus what Whisper reported about it (see
    :class:`~flow.groq_client.TranscriptionResult`), threaded through to the
    cleanup stage's language-aware skip heuristic
    (:func:`~flow.cleanup.should_skip_cleanup`) and its confidence check."""

    text: str
    language: str | None
    #: False when Whisper's own confidence signals say the decode was shaky;
    #: cleanup then leaves the text alone rather than letting a model write a
    #: fluent, confident-sounding rewrite of something it also can't read.
    confident: bool = True
    #: Durations measured during capture, in milliseconds: ``audio_ms`` is the
    #: whole held clip, ``speech_ms`` only what survived VAD trimming (the
    #: basis for WPM and time-saved in :mod:`flow.analytics`). None on paths
    #: that never measured them — e.g. the streamed tail, where the chunks are
    #: plain text by the time they merge; the caller recomputes from the ring
    #: buffer in that case.
    speech_ms: float | None = None
    audio_ms: float | None = None


@dataclass
class PendingTransformResult:
    """One reviewed rewrite's result, held between the review panel opening
    and the user acting on it (Apply, Copy, Retry, Undo, or simply
    dismissing it). Every path through :meth:`Engine._run_command` --
    a shortcut-fired transform or a spoken voice command alike -- lands
    here; ``transform_id`` is ``""`` for a spoken command, which has no
    stored :class:`~sway.transforms.Transform` to point at.

    Mutable (not frozen) on purpose: Retry replaces ``transformed`` in place
    for a panel that is already open, and Apply flips ``applied`` so a
    second Apply is a no-op and Undo knows there is something to undo.
    """

    transform_id: str
    transform_name: str
    original: str
    transformed: str
    instruction: str
    focused_process: str | None
    activation: Activation | None
    #: What the rewrite was allowed to do, so Retry can re-check a fresh
    #: response the same way the first attempt was checked -- a stored
    #: transform recomputes this from its own flags, but a spoken command
    #: has nothing to recompute it from.
    expectation: OutputExpectation = SPOKEN_COMMAND_EXPECTATION
    #: The :func:`~sway.cleanup.unsafe_reason` token this result tripped, if
    #: any. Advisory here, never a veto -- nothing on this path reaches the
    #: document without an explicit Apply, so a tripped guardrail is
    #: rendered as a note (see :func:`~sway.cleanup.advisory_note`) rather
    #: than discarding the result the way it would on an auto-paste path.
    guardrail_reason: str = ""
    applied: bool = False


class Engine:
    """Owns every long-lived resource for one run of the app."""

    def __init__(self, config: Config, tracker: LatencyTracker) -> None:
        # The user's saved settings are layered over the resolved config
        # before anything reads it, so the hotkey listener, the mic and the
        # retention windows below are all built from the values the user
        # actually chose in the UI rather than from config.toml's.
        settings_store = SettingsStore.open(config.data_dir)
        config = apply_to_config(config, settings_store.get())

        self._config = config
        self._tracker = tracker
        self._platform: PlatformAdapter = get_platform()

        self._ring = RingBuffer(
            capacity_frames=config.audio.ring_seconds * config.audio.sample_rate,
            sample_width=2,
            channels=config.audio.channels,
        )
        self._capture = AudioCapture(self._ring, config.audio)
        self._vad = VadTrimmer(
            sample_rate=config.audio.sample_rate,
            frame_ms=config.audio.vad_frame_ms,
            mode=config.audio.vad_mode,
            padding_ms=config.audio.vad_padding_ms,
        )
        #: One OS-level hook (one keyboard listener, one mouse listener) for
        #: every binding in the app at once: dictation's, the voice command's,
        #: hands-free's, and every transform's -- however many each holds.
        #: Rebuilt from settings by :meth:`_rebind_all_bindings` at startup
        #: and again whenever any of them changes. Constructed here, ahead of
        #: TextInjector/SelectionReader below, because both need
        #: self._bindings.suspended to guard their own synthetic keystrokes.
        self._bindings = BindingListener(modifier_probe=self._platform.modifier_keys_down)
        #: Every configured binding's registration status, rebuilt on every
        #: :meth:`_rebind_all_bindings` call -- what `describe()` exposes to
        #: the dashboard's read-only "Registered shortcuts" table.
        self._binding_ledger: list[BindingRecord] = []
        #: slot_id -> (fire_count, last_fired_at as time.time()). Survives a
        #: rebind for any slot id that still exists afterward -- see
        #: _rebind_all_bindings's pruning.
        self._binding_fires: dict[str, tuple[int, float]] = {}
        #: Guards against two shortcut-run transforms overlapping -- see
        #: _run_transform, which would otherwise reset _selected_text under
        #: a still-running capture.
        self._transform_lock = threading.Lock()
        self._transform_running = False
        self._injector = TextInjector(
            self._platform,
            restore_delay_ms=config.paste.restore_delay_ms,
            suspend_input=self._bindings.suspended,
        )
        self._selection_reader = SelectionReader(
            self._platform,
            timeout_s=config.commands.selection_timeout_s,
            suspend_input=self._bindings.suspended,
            # The same probe self._bindings uses to heal its own hook state
            # (see BindingListener's own modifier_probe= above), here doing
            # a different job: not sending the synthetic Ctrl+C while the
            # user is still holding the Shift/Alt/Win of the chord that
            # fired the transform. Every shipped transform shortcut is
            # Ctrl+Alt+N, and Ctrl+Alt+C is not copy anywhere.
            modifier_probe=self._platform.modifier_keys_down,
        )
        #: The same store the config above was built from, handed to the
        #: dashboard so both read one source rather than two that could drift.
        #: Assigned before the bar, which reads it for its dock position.
        self._settings = settings_store
        self._bar = self._build_bar() if config.overlay.enabled else NullBar()
        self._network = NetworkLoop()
        self._groq: GroqClient | None = None
        self._cleanup_stats = CleanupStats()
        self._dictionary: DictionaryStore | None = None
        self._history: HistoryStore | None = None
        self._history_window: HistoryWindow | None = None
        #: The review panel for a shortcut-run transform's before/after, and
        #: the result it is currently showing (if any). Built once, on the
        #: Tk thread, and reused across runs -- see _present_transform_result.
        self._transform_panel: TransformReviewPanel | None = None
        self._pending_transform: PendingTransformResult | None = None
        self._tray: TrayIcon | None = None
        self._dashboard: DashboardServer | None = None
        self._window: AppWindow | None = None
        #: The last failure surfaced to the user, for Home's status card.
        #: A dict rather than an exception so it can cross the HTTP boundary
        #: unchanged.
        self._last_error: dict[str, Any] | None = None
        #: 0-3 mic level bucket, written by the capture callback path and read
        #: by /api/status. A plain int assignment is atomic under the GIL, so
        #: this needs no lock and the callback never blocks (CLAUDE.md).
        self._input_level = 0
        self._hotkey_down = False
        self._command_key_down = False
        #: When the last too-short hold happened, and how many in a row.
        #: A single tap gets a gentle correction; a second in quick
        #: succession gets a more explicit one, because the first evidently
        #: did not land.
        self._last_tap_at = 0.0
        self._tap_streak = 0
        #: Successful dictations this session. Coaching retires after a few:
        #: guidance that never stops is just instructional chrome.
        self._success_count = 0
        #: The most recent pasted text, for the bar's paste/copy actions.
        #: Memory only -- history.db holds the durable record.
        self._last_transcript = ""
        self._level_pump: threading.Thread | None = None
        #: Whether the hotkeys are armed. The mic stream itself stays open
        #: either way -- CLAUDE.md requires it to open once and stay open, and
        #: closing it on pause would reintroduce exactly the open-on-demand
        #: latency the architecture exists to avoid. Pausing disarms the
        #: hotkey, so the ring buffer keeps filling and is simply never read.
        self._listening = True
        self._listening_lock = threading.Lock()

        #: Coalesce however many hold-shaped bindings one action has into a
        #: single start/stop pair -- see sway.hotkey.RefCountedGate.
        self._dictation_gate = RefCountedGate()
        self._command_gate = RefCountedGate()

        self._preroll_frames = round(config.audio.preroll_ms / 1000.0 * config.audio.sample_rate)
        self._start_frame = 0
        self._press_time = 0.0
        self._release_time = 0.0
        self._focused_process: str | None = None
        self._stream_session: ChunkStreamer | None = None
        # Recent high-confidence language verdicts, newest last. Whisper's own
        # detection is close to random under about two seconds of speech
        # ("Hello" has come back as Hindi, a proper noun as Korean), so a
        # one-word utterance must not be allowed to pick the model or the
        # prompt on its own. People rarely switch language between
        # consecutive dictations, which makes the recent past a far better
        # prior than a coin flip. Guarded by _language_lock because the bar
        # thread reads it while a worker writes it.
        self._language_history: deque[str] = deque(maxlen=_LANGUAGE_HISTORY)
        self._language_lock = threading.Lock()

        self._command_press_time = 0.0
        self._command_start_frame = 0
        self._command_focused_process: str | None = None
        self._selection_ready = threading.Event()
        self._selected_text = ""

        # -- hands-free dictation (Phase 6): tap to start, tap again to stop.
        # See _configure_hands_free for how the binding (a bare held key or
        # combo, double-tapped or not, vs. a chord) maps onto self._bindings.
        self._hands_free_active = False
        self._hands_free_tracker: TapModeTracker | DoubleTapTracker | None = None
        self._hands_free_session: ChunkStreamer | None = None
        self._hands_free_start_frame = 0
        self._hands_free_focused_process: str | None = None
        self._hands_free_timer: threading.Thread | None = None

        # Any binding tap-shaped rather than hold-shaped (the reference's "+"
        # affordance): a tap fires once, so unlike a held key or combo it
        # starts/stops a session rather than being held -- driven by the same
        # TapModeTracker hands-free uses, and protected by the same safety
        # ceiling (_hands_free_max_seconds is reused here as "how long a
        # *toggle-shaped* session may run before being force-stopped", not
        # just a hands-free-specific concept) so a missed second tap can
        # never leave the app looking stuck recording.
        self._dictation_chord_tracker = TapModeTracker(
            max_active_s=float(config.hotkey.hands_free_max_seconds)
        )
        self._command_chord_tracker = TapModeTracker(
            max_active_s=float(config.hotkey.hands_free_max_seconds)
        )
        self._dictation_started_via_chord = False
        self._command_started_via_chord = False
        self._activation_timer: threading.Thread | None = None
        self._stopping = False
        #: One user intention, one execution. Every press mints a token here
        #: and every release claims it, so a second release with no press
        #: behind it cannot re-dispatch the same audio. See sway.activation.
        self._activations = ActivationLedger()

        self._rebind_all_bindings()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Opens the mic, starts the network loop and Groq client, arms the hotkey(s)."""
        self._warn_if_another_instance_is_live()
        self._capture.start()
        self._network.start()
        # A missing key is a state to be *in*, not a reason to refuse to
        # start. Onboarding runs inside the app window, so the app has to be
        # running for the user to be able to give it a key at all. Everything
        # except transcription works meanwhile, and Home says plainly why.
        self._ensure_groq_client()
        if self._config.dictionary.enabled:
            try:
                self._dictionary = DictionaryStore.open(self._config.dictionary_db_path)
            except DictionaryStoreError as exc:
                # Diagnostic only: dictation still works, just without the bias.
                logger.error("personal dictionary disabled: %s", exc)
        if self._config.history.enabled:
            self._start_history()
        self._bindings.start()
        self._start_activation_timeout_pump()
        if self._config.tray.enabled:
            self._tray = TrayIcon(
                on_show_history=self._open_history_window,
                on_open_dashboard=self._open_dashboard,
                on_open_settings=self._open_settings,
                on_view_logs=self._open_logs,
                on_quit=self._bar.stop,
                is_listening=lambda: self.is_listening,
                set_listening=self.set_listening,
            )
            if not self._tray.start():
                self._tray = None
        # Advertise before announcing readiness: a second launch racing the
        # first would otherwise find no instance file and show the "already
        # running" dialog instead of focusing us.
        self._publish_instance()
        # The bar's opening posture. _refresh_bar_hint was written to coach a
        # first-run user and was only ever called *after* a successful paste,
        # where its first branch clears the hint -- so the coaching it exists
        # for had never once rendered.
        self._bar.set_listening(self.is_listening)
        self._bar.set_can_paste(False)
        self._refresh_bar_hint()
        dictation_label = self._primary_binding_label(self._effective_dictation_bindings())
        if dictation_label:
            logger.info("Sway ready. Hold %s to dictate.", dictation_label)
        else:
            logger.info("Sway ready, but dictation has no binding -- add one in Settings.")
        command_label = self._primary_binding_label(self._effective_command_bindings())
        if command_label:
            logger.info(
                "Hold %s with text selected to run a voice command.", command_label
            )

    def _start_history(self) -> None:
        try:
            store = HistoryStore.open(self._config.history_db_path)
            removed = store.prune(self._config.history.retention_days)
            if removed:
                logger.info("pruned %d history entry/entries older than %d day(s)", removed, self._config.history.retention_days)
        except HistoryStoreError as exc:
            # Diagnostic only: dictation still works, just without history.
            logger.error("history disabled: %s", exc)
            return
        self._history = store

    def _require_groq(self) -> GroqClient:
        """The Groq client, or a clear failure.

        Raises :class:`~flow.groq_client.GroqError` rather than asserting: no
        key configured is an ordinary state the app can be in now (onboarding
        runs inside the running app), so it must produce the same
        user-visible error path as any other Groq failure -- not an
        AssertionError that vanishes under ``-O``.
        """
        client = self._groq
        if client is None:
            raise GroqError("No Groq API key is set. Add one in Settings.")
        return client

    def _ensure_groq_client(self) -> bool:
        """Build the Groq client if a key is configured. Never raises.

        Called at startup and again whenever the key changes, so a user who
        finishes onboarding can dictate immediately rather than restarting
        the app to pick up their own key.
        """
        if self._groq is not None:
            return True
        if not self._config.groq_api_key:
            logger.info("no Groq API key yet; transcription is unavailable until one is set")
            self._record_error(
                kind="no_key",
                headline="Paused — no API key",
                detail="Sway cannot transcribe until a Groq key is set.",
                actionable=True,
            )
            return False
        try:
            client = self._network.submit(self._build_groq_client()).result(timeout=10.0)
        except Exception:
            logger.exception("could not create the Groq client")
            return False
        self._groq = client
        self._network.submit(client.warm_up())
        self._clear_error()
        return True

    def _reload_api_key(self) -> None:
        """Pick up a key the user just entered, without a restart."""
        from sway.config import Secret
        from sway import credentials

        key, _source = credentials.resolve_key()
        if key == self._config.groq_api_key.reveal():
            return

        self._config = replace(self._config, groq_api_key=Secret(key))
        # Drop any existing client: it holds the old key in a header set at
        # construction time, so it cannot be reused for a different one.
        old, self._groq = self._groq, None
        if old is not None:
            try:
                self._network.submit(old.aclose())
            except Exception:
                logger.debug("could not close the previous Groq client", exc_info=True)
        self._ensure_groq_client()

    async def _build_groq_client(self) -> GroqClient:
        return GroqClient(
            self._config.groq_api_key,
            base_url=self._config.groq.base_url,
            timeout_s=self._config.groq.timeout_s,
            connect_timeout_s=self._config.groq.connect_timeout_s,
            max_retries=self._config.groq.max_retries,
        )

    def run_ui(self) -> None:
        """Own the process's main thread until shutdown. Call once, from main.

        Which loop gets the main thread is forced on us: ``webview.start()``
        refuses to run anywhere else, while Tk only needs one *consistent*
        thread. So when a native window is possible the webview takes main
        and the bar runs beside it; otherwise the bar takes main and the
        window falls back to a browser.

        Either way the bar exists for the whole session, because it has to be
        there for every dictation whether or not a window is open.
        """
        window = self._prepare_window()
        if window is None:
            self._bar.run()
            return

        bar_thread = threading.Thread(target=self._bar.run, name="sway-bar", daemon=True)
        bar_thread.start()

        # First run opens itself. Someone who has just installed this has no
        # reason to know there is a tray icon, and "unzip, double-click,
        # dictate" cannot survive a step where the app is running but
        # invisible. Afterwards it stays out of the way and waits to be asked.
        if not self._settings.get().onboarding_complete:
            threading.Thread(
                target=self._show_window_when_ready, name="sway-first-run", daemon=True
            ).start()

        try:
            window.serve_forever()
        finally:
            # The webview loop returning means shutdown, so take the bar with
            # it rather than leaving a dot on screen with nothing behind it.
            self._bar.stop()
            bar_thread.join(timeout=3.0)

    #: Kept as the previous name so a caller written against it still works.
    run_overlay = run_ui

    def _show_window_when_ready(self) -> None:
        """Show the window once its runtime has finished starting.

        ``serve_forever`` has to be running before a window can be shown, and
        it starts on the thread that called it, so this waits briefly rather
        than racing it. Gives up quietly: a first run that fails to raise the
        window still has a working tray icon and bar.
        """
        for _ in range(40):
            time.sleep(0.25)
            if self._window is not None and self._window.show():
                return
        logger.info("could not raise the window for first run")

    def _prepare_window(self) -> AppWindow | None:
        """Build the hidden window, if this machine can have one.

        Runs before the loop starts because ``webview.start()`` needs a window
        to start with and cannot be called twice -- so the window is created
        at launch and merely shown later.
        """
        if not self._config.overlay.enabled:
            return None
        server = self._ensure_dashboard()
        if server is None:
            return None
        window = AppWindow(server.url, title=APP_NAME)
        if not window.prepare():
            return None
        self._window = window
        return window

    def stop(self) -> None:
        """Idempotent; safe to call even if :meth:`start` only partly succeeded."""
        self._stopping = True
        for label, action in (
            ("bindings", self._bindings.stop),
            ("tray", self._stop_tray),
            ("window", self._stop_window),
            ("dashboard", self._stop_dashboard),
            ("capture", self._capture.stop),
            ("network", self._stop_network),
            ("dictionary", self._stop_dictionary),
            ("history", self._stop_history),
            ("overlay", self._bar.stop),
            # Withdrawn *last*, after every hotkey hook is down. Withdrawing it
            # first -- as this used to -- opens a window in which the
            # advertisement is gone while the hooks are still armed, so a
            # launch arriving during a slow shutdown sees "nothing running",
            # starts a rival, and both copies then answer the same keypress.
            # A stale file is already handled (instance.read checks the pid is
            # alive); a *missing* file while we are still listening is not.
            ("instance file", self._withdraw_instance),
        ):
            try:
                action()
            except Exception:
                logger.exception("error stopping %s", label)

    def _stop_network(self) -> None:
        if self._groq is not None and self._network.is_running:
            try:
                self._network.submit(self._groq.aclose()).result(timeout=5.0)
            except Exception:
                logger.exception("error closing Groq client")
        self._network.stop()

    def _stop_dictionary(self) -> None:
        if self._dictionary is not None:
            self._dictionary.close()

    def _stop_history(self) -> None:
        if self._history is not None:
            self._history.close()

    def _warn_if_another_instance_is_live(self) -> None:
        """Say so, loudly, if a copy of Sway is already running.

        ``Sway.exe`` refuses a second launch behind a named mutex, but that is
        one entry point among several -- ``python -m sway run`` takes no mutex
        at all, and a developer or a launch-at-login entry can reach it. Two
        engines mean two hotkey hooks on one key: every press is transcribed
        twice, billed twice against the free tier, and pasted twice.

        A warning rather than a refusal, deliberately. This reads a pid out of
        a file, and being wrong in the refusing direction would leave the user
        unable to start their dictation app at all. The log line is what makes
        the condition diagnosable in ten seconds instead of by comparing
        databases -- which is how long it took the first time.
        """
        try:
            info = instance.read(self._config.data_dir)
        except Exception:
            logger.debug("could not check for another running instance", exc_info=True)
            return
        if info is None or info.pid == os.getpid():
            return
        logger.warning(
            "another Sway process (pid %s) appears to be running and advertising at %s. "
            "Two copies both hook the dictation key, so every press is transcribed and "
            "pasted twice. Quit the other copy if you did not mean to run two.",
            info.pid,
            info.port,
        )

    def _withdraw_instance(self) -> None:
        """Remove the advertisement before the port goes away, so a launch
        racing this shutdown never finds a file pointing at a dead socket."""
        instance.withdraw(self._config.data_dir)

    def _stop_dashboard(self) -> None:
        if self._dashboard is not None:
            self._dashboard.stop()
            self._dashboard = None

    def _stop_tray(self) -> None:
        if self._tray is not None:
            self._tray.stop()

    def _stop_window(self) -> None:
        if self._window is not None:
            self._window.destroy()
            self._window = None

    # -- history window (Phase 5): opened from the tray, shown on the Tk thread

    #: Overlay state -> tray state. The two surfaces answer different
    #: questions -- the overlay is what the user watches mid-dictation, the
    #: tray answers "is it even on?" when no overlay is showing -- but they
    #: must never disagree, so every transition goes through _set_ui_state.
    _TRAY_STATE_FOR: dict[BarState, TrayState] = {
        BarState.IDLE: TrayState.IDLE,
        BarState.RECORDING: TrayState.LISTENING,
        BarState.THINKING: TrayState.PROCESSING,
        BarState.INJECTING: TrayState.PROCESSING,
        BarState.ERROR: TrayState.ERROR,
        BarState.PAUSED: TrayState.DISABLED,
        # Coaching is not engine state. The tray keeps saying "ready",
        # because that is what is true while the bar is teaching.
        BarState.COACH: TrayState.IDLE,
    }

    def _set_ui_state(
        self, state: BarState, *, detail: str = "", kind: str = ""
    ) -> None:
        """Drive the overlay and the tray icon together.

        A paused engine always shows DISABLED in the tray regardless of the
        overlay state it was given, so the two surfaces cannot disagree about
        whether the hotkey is armed.

        ``kind`` is the machine-readable failure category from
        :func:`_describe_failure`. It was previously computed on every error
        path and then dropped; passing it through is what lets the bar offer
        the one action that actually helps.
        """
        # A dictation in flight owns the bar. Without this, holding the
        # hotkey again while the previous transcription is still running lets
        # that finishing worker paint "Done - 12 words" over a live recording
        # -- the bar would be reporting the *previous* utterance while the
        # user watched themselves speak the next one.
        if (self._hotkey_down or self._hands_free_active) and state not in (
            BarState.RECORDING,
            BarState.PAUSED,
        ):
            logger.debug(
                "suppressed a %s bar update arriving during a live hold", state
            )
            return
        self._bar.set_state(state, detail, kind=kind)
        if self._tray is None:
            return
        tray_state = self._TRAY_STATE_FOR.get(state, TrayState.IDLE)
        if not self.is_listening:
            tray_state = TrayState.DISABLED
        self._tray.set_state(tray_state)

    def _open_logs(self) -> None:
        """Open the log file in whatever the OS uses for .log files.

        Best-effort and non-blocking. The point is that "View Logs" in the
        tray beats telling a user to find %LOCALAPPDATA% by hand, which is
        the only other option in a build with no console.
        """
        path = self._config.log_file_path
        try:
            if not path.exists():
                # An empty log is a confusing thing to open; say so instead.
                self._platform.show_message(
                    APP_NAME, f"No log file yet.\n\nIt will appear at:\n{path}"
                )
                return
            self._platform.open_url(path.as_uri())
        except Exception:
            logger.exception("could not open the log file")

    def _open_history_window(self) -> None:
        """Called from the tray icon's own thread; never touches Tk directly."""
        if self._history is None:
            logger.info("history window requested, but [history].enabled = false")
            return
        self._bar.run_on_ui_thread(self._show_history_window)

    def _show_history_window(self, root: tk.Tk) -> None:
        """Runs on the Tk thread only, via Overlay.run_on_ui_thread."""
        assert self._history is not None
        if self._history_window is None:
            self._history_window = HistoryWindow(root, self._history)
        self._history_window.show()

    # -- binding gates: collapse N hold-shaped bindings into one start/stop --
    #
    # A dictation or command binding is now a *list*, and more than one entry
    # can be hold-shaped (e.g. both ctrl_r and ctrl+cmd bound to dictation).
    # These wrappers are what BindingListener actually calls; the real
    # _on_press/_on_release only run on the first binding to engage / the
    # last one to disengage, via sway.hotkey.RefCountedGate. See its
    # docstring for why this matters -- without it, a second binding
    # engaging mid-recording would restart the capture's bookkeeping and
    # silently truncate what the user already said.

    def _dictation_binding_engaged(self, pressed_at: float) -> None:
        if self._dictation_gate.acquire():
            self._on_press(pressed_at)

    def _dictation_binding_disengaged(self) -> None:
        if self._dictation_gate.release():
            self._on_release()

    def _command_binding_engaged(self, pressed_at: float) -> None:
        if self._command_gate.acquire():
            self._on_command_press(pressed_at)

    def _command_binding_disengaged(self) -> None:
        if self._command_gate.release():
            self._on_command_release()

    # -- dictation hotkey callbacks (called from the pynput listener thread) --

    def _on_press(self, pressed_at: float | None = None) -> None:
        now = time.monotonic()
        # The true physical press instant, even when the engage itself was
        # deferred by sway.hotkey's prefix grace -- so should_process below
        # measures the real hold duration, not the post-grace remainder.
        self._press_time = now if pressed_at is None else pressed_at
        # Read by /api/status so onboarding can show the key lighting up as
        # it is physically held. A plain bool assignment, so the listener
        # thread is not slowed by it.
        self._hotkey_down = True
        self._activations.begin(DICTATION)
        self._start_frame = preroll_start_frame(
            frames_before(self._ring.mark(), now - self._press_time, self._config.audio.sample_rate),
            self._preroll_frames,
        )
        # Captured at press time, when the target app is unambiguously the one
        # the user is dictating into; PlatformAdapter guarantees this never
        # raises, so no try/except is needed on this hot path.
        self._focused_process = self._platform.focused_process_name()
        # Metering runs only while the key is held, so the steady-state audio
        # callback stays a bounds-checked memcpy and nothing more.
        self._capture.set_level_metering(True)
        self._start_level_pump()
        self._set_ui_state(BarState.RECORDING)
        self._stream_session = (
            self._start_streaming(self._start_frame) if self._streaming_wanted() else None
        )

    def _on_release(self) -> None:
        release_time = time.monotonic()
        # Claimed first, before any state is touched. A release with nothing
        # outstanding is a duplicate -- a doubled OS hook, a toggle racing its
        # own safety timeout, a rival process's event -- and dispatching it
        # would transcribe and paste the same audio a second time. `_press_time`
        # and `_start_frame` are still set from the real press, so there is
        # nothing here to stop it except this.
        activation = self._activations.claim(DICTATION)
        if activation is None:
            logger.warning(
                "ignoring a dictation release with no press behind it "
                "(a duplicate event; this would have pasted twice)"
            )
            return
        self._hotkey_down = False
        self._dictation_started_via_chord = False
        self._capture.set_level_metering(False)
        self._input_level = 0
        self._bar.set_level(0)
        held_ms = (release_time - self._press_time) * 1000.0
        session, self._stream_session = self._stream_session, None
        if not should_process(held_ms, self._config.hotkey.min_hold_ms):
            if session is not None:
                session.stop(timeout=0.5)
            self._coach_tap(release_time)
            return
        self._set_ui_state(BarState.THINKING, detail="Transcribing")
        # Hand off immediately: the listener thread must stay free for the
        # next key event, and this thread may block on the network.
        threading.Thread(
            name="flow-dictation",
            target=self._process,
            args=(
                self._start_frame,
                self._focused_process,
                session,
                release_time,
                held_ms,
                self._press_time,
                activation,
            ),
            daemon=True,
        ).start()

    def _coach_tap(self, at: float) -> None:
        """Answer a too-short hold instead of silently ignoring it.

        This is the single most common first-run mistake -- a dictation
        hotkey is the one control on the desktop that a tap does *not*
        operate -- and it previously produced no feedback whatsoever, which
        reads as the app being broken rather than as the gesture being wrong.

        A repeat inside ten seconds escalates the wording, because the first
        phrasing evidently did not land -- and a repeat is answered for every
        user, however experienced, because it is evidence of someone trying
        rather than of a key being brushed.
        """
        self._tap_streak = self._tap_streak + 1 if at - self._last_tap_at < 10.0 else 1
        self._last_tap_at = at
        repeated = self._tap_streak >= 2

        # A first-run user is told straight away. An experienced one is not:
        # a single stray tap is almost always the key being brushed, and
        # explaining the product every time it happens is noise. But a
        # *second* tap in ten seconds means they are trying to do something
        # and it is not working, and that is worth answering no matter how
        # long they have used it.
        if not (repeated or self._should_coach()):
            self._set_ui_state(BarState.IDLE)
            return

        key = self._primary_binding_label(self._effective_dictation_bindings()) or "your hotkey"
        if repeated:
            text = f"Hold [[{key}]] down, speak, then let go"
        else:
            text = f"Don't tap — hold [[{key}]] while you speak"
        try:
            self._bar.coach(text)
        except Exception:
            logger.debug("could not show the tap coach", exc_info=True)
            self._set_ui_state(BarState.IDLE)

    def _should_coach(self) -> bool:
        """Whether the user still needs teaching.

        Two independent signals, and both have to say yes: they have not
        finished onboarding, and they have not already succeeded several
        times this session. Someone who dictates fluently and fumbles one
        keypress does not need to be told how the product works.
        """
        if self._success_count >= 3:
            return False
        try:
            return not self._settings.get().onboarding_complete
        except Exception:
            logger.debug("could not read onboarding state for coaching", exc_info=True)
            return False

    # -- streaming (Phase 4): background chunks while the key is held ----

    def _start_streaming(self, start_frame: int) -> ChunkStreamer:
        streaming = self._config.streaming
        session = ChunkStreamer(
            ring=self._ring,
            vad=self._vad,
            encode=lambda pcm: encode_flac(
                pcm, self._config.audio.sample_rate, self._config.audio.channels
            ),
            submit_transcribe=self._submit_chunk_transcribe,
            start_frame=start_frame,
            sample_rate=self._config.audio.sample_rate,
            chunk_seconds=streaming.chunk_seconds,
            overlap_seconds=streaming.overlap_seconds,
            poll_interval_s=streaming.poll_interval_s,
        )
        session.start()
        return session

    def _submit_chunk_transcribe(self, flac_bytes: bytes) -> Future[TranscriptionResult]:
        self._require_groq()
        prompt_bias = self._build_prompt_bias()
        return self._network.submit(
            self._groq.transcribe(
                flac_bytes,
                language=self._config.groq.language,
                prompt=prompt_bias or None,
                model=self._stt_model_for(self._language_prior()),
            )
        )

    # -- command hotkey callbacks (Phase 5; called from the listener thread) --

    def _on_command_press(self, pressed_at: float | None = None) -> None:
        now = time.monotonic()
        self._command_press_time = now if pressed_at is None else pressed_at
        self._command_key_down = True
        self._activations.begin(COMMAND)
        self._command_start_frame = preroll_start_frame(
            frames_before(
                self._ring.mark(), now - self._command_press_time, self._config.audio.sample_rate
            ),
            self._preroll_frames,
        )
        self._command_focused_process = self._platform.focused_process_name()
        self._set_ui_state(BarState.RECORDING)
        selection_ready = threading.Event()
        self._selection_ready = selection_ready
        self._selected_text = ""
        # The Ctrl+C round trip can take tens of milliseconds waiting on the
        # target app; hand it off immediately so the listener thread stays
        # free, same reasoning as the dictation hotkey's release handoff.
        threading.Thread(
            target=self._capture_selection,
            args=(selection_ready,),
            name="flow-selection",
            daemon=True,
        ).start()

    def _capture_selection(self, ready: threading.Event) -> None:
        try:
            self._selected_text = self._selection_reader.read()
        except Exception:
            logger.exception("could not read the text selection for a voice command")
            self._selected_text = ""
        finally:
            ready.set()

    def _on_command_release(self) -> None:
        # Same rule as dictation: a release with no press behind it is a
        # duplicate event, and running it would transform the selection twice.
        activation = self._activations.claim(COMMAND)
        if activation is None:
            logger.warning(
                "ignoring a command release with no press behind it (a duplicate event)"
            )
            return
        self._command_key_down = False
        self._command_started_via_chord = False
        held_ms = (time.monotonic() - self._command_press_time) * 1000.0
        if not should_process(held_ms, self._config.hotkey.min_hold_ms):
            self._set_ui_state(BarState.IDLE)
            return
        self._set_ui_state(BarState.THINKING)
        threading.Thread(
            name="flow-command",
            target=self._process_command,
            args=(
                self._command_start_frame,
                self._command_focused_process,
                self._selection_ready,
                "",
                "command",
                SPOKEN_COMMAND_EXPECTATION,
                activation,
            ),
            daemon=True,
        ).start()

    # -- hands-free dictation (Phase 6): tap to start, tap again to stop --

    # -- effective bindings: Settings' override (if any) layered over
    # config.toml's compiled-in default, per sway.hotkey.effective_bindings --

    def _effective_dictation_bindings(self) -> tuple[str, ...]:
        hotkey_cfg = self._config.hotkey
        return effective_bindings(self._settings.get().dictation_bindings, hotkey_cfg.key, hotkey_cfg.key_chord)

    def _effective_command_bindings(self) -> tuple[str, ...]:
        hotkey_cfg = self._config.hotkey
        return effective_bindings(
            self._settings.get().command_bindings, hotkey_cfg.command_key, hotkey_cfg.command_key_chord
        )

    def _effective_hands_free_bindings(self) -> tuple[str, ...]:
        hotkey_cfg = self._config.hotkey
        return effective_bindings(
            self._settings.get().hands_free_bindings, hotkey_cfg.hands_free_key, hotkey_cfg.hands_free_chord
        )

    @staticmethod
    def _primary_binding_label(bindings: tuple[str, ...]) -> str:
        """The first binding's display label, or ``""`` if there are none.

        Best-effort: a binding that somehow fails to parse (it should
        always parse -- everything reaching Settings has already been
        validated once) is skipped rather than raised, since this only ever
        feeds a log line or a coaching hint.
        """
        for text in bindings:
            try:
                return parse_binding(text).pretty()
            except BindingError:
                continue
        return ""

    def _configure_hands_free(self) -> tuple[bool, tuple[str, ...]]:
        """Establish whether hands-free is usable right now, and force-stop
        an active session if it just stopped being usable.

        Returns ``(usable, bindings)`` for :meth:`_rebind_all_bindings` to
        wire into the shared listener -- ``bindings`` is empty whenever
        ``usable`` is ``False``, so a caller never needs to check both.

        Never tears down a session silently: if hands-free is being turned
        off while a session is active, that session is force-stopped and
        processed first -- the mic must never be left "hot" behind a
        binding that no longer exists to stop it. If only the *binding*
        (which keys/chord, or the double-tap timing) changes while a session
        is active, the running session is left alone; the new binding takes
        effect on the next toggle rather than retroactively reinterpreting a
        session that is already speaking.
        """
        enabled = self._settings.get().hands_free_enabled
        bindings = self._effective_hands_free_bindings()
        usable = enabled and bool(bindings)

        if not usable and self._hands_free_active:
            self._stop_hands_free(timed_out=False)

        if usable:
            hotkey_cfg = self._config.hotkey
            if hotkey_cfg.hands_free_double_tap:
                self._hands_free_tracker = DoubleTapTracker(
                    max_active_s=float(hotkey_cfg.hands_free_max_seconds)
                )
            else:
                self._hands_free_tracker = TapModeTracker(
                    max_active_s=float(hotkey_cfg.hands_free_max_seconds)
                )
        else:
            self._hands_free_tracker = None

        return usable, bindings if usable else ()

    def _traced(self, slot_id: str, binding: Binding, fn: Callable[[float], None]) -> Callable[[float], None]:
        """Wrap ``fn`` (an ``on_engaged``/``on_fire`` callback) to record a
        fire in ``self._binding_fires`` and log it, without the callback
        itself needing to know its own slot id.

        Wrapping once here, at registration, covers both a hold-shaped
        binding's engage and a tap-shaped one's fire -- :meth:`register`
        below passes the same ``on_engaged`` value into both, exactly as
        :class:`~sway.hotkey.BindingListener` itself treats them
        symmetrically (``on_engaged``/``on_fire`` both take the press
        instant).
        """

        def wrapped(pressed_at: float) -> None:
            count, _ = self._binding_fires.get(slot_id, (0, 0.0))
            self._binding_fires[slot_id] = (count + 1, time.time())
            logger.info("binding %s (%s) fired", slot_id, binding)
            fn(pressed_at)

        return wrapped

    def _rebind_all_bindings(self) -> None:
        """Rebuild the one shared :class:`~sway.hotkey.BindingListener`'s
        registry: every dictation, command, hands-free, and transform
        binding at once.

        Called at construction and again from :meth:`_apply_settings`
        whenever any of them changes. The OS hooks stay up throughout (see
        :meth:`~sway.hotkey.BindingListener.rebind`), so a binding the user
        just set works on the very next keystroke rather than needing a
        restart.

        Never raises. A binding string that will not parse is logged and
        skipped rather than taken as a reason to leave every other binding
        unbound -- one bad entry must not disable the rest. Also rebuilds
        ``self._binding_ledger`` (every configured binding's registration
        status -- registered, invalid, or duplicate; see
        :meth:`describe`) and prunes ``self._binding_fires`` of slot ids
        that no longer exist, so a binding's fire history survives a
        rebind that did not touch it.
        """
        settings = self._settings.get()
        dictation = self._effective_dictation_bindings()
        command = self._effective_command_bindings()
        _hands_free_usable, hands_free = self._configure_hands_free()

        hold_bindings: dict[str, tuple[Binding, Callable[[float], None], Callable[[], None] | None]] = {}
        tap_bindings: dict[str, tuple[Binding, Callable[[float], None]]] = {}
        ledger: list[BindingRecord] = []

        def register(
            group: str,
            values: Sequence[str],
            *,
            label: str,
            on_engaged: Callable[[float], None],
            on_disengaged: Callable[[], None] | None,
        ) -> None:
            for index, text in enumerate(values):
                slot_id = f"{group}:{index}"
                try:
                    binding = parse_binding(text)
                except BindingError as exc:
                    logger.warning(
                        "%s binding %r is unusable (%s); leaving it unbound", group, text, exc
                    )
                    ledger.append(
                        BindingRecord(
                            slot_id=slot_id,
                            owner=group,
                            label=label,
                            value=text,
                            pretty="",
                            shape="",
                            status="invalid",
                            detail=str(exc),
                        )
                    )
                    continue
                traced = self._traced(slot_id, binding, on_engaged)
                if binding.shape == "hold":
                    hold_bindings[slot_id] = (binding, traced, on_disengaged)
                else:
                    tap_bindings[slot_id] = (binding, traced)
                ledger.append(
                    BindingRecord(
                        slot_id=slot_id,
                        owner=group,
                        label=label,
                        value=text,
                        pretty=binding.pretty(),
                        shape=binding.shape,
                        status="registered",
                        detail="",
                    )
                )

        register(
            "dictation",
            dictation,
            label="the dictation hotkey",
            on_engaged=self._dictation_binding_engaged,
            on_disengaged=self._dictation_binding_disengaged,
        )
        register(
            "command",
            command,
            label="the voice-command hotkey",
            on_engaged=self._command_binding_engaged,
            on_disengaged=self._command_binding_disengaged,
        )
        register(
            "hands_free",
            hands_free,
            label="hands-free dictation",
            on_engaged=self._toggle_hands_free,
            on_disengaged=None,
        )

        try:
            transforms = settings.transforms
        except Exception:
            logger.exception("could not read transforms; leaving shortcuts unbound")
            transforms = ()
        for transform in transforms_module.enabled_with_shortcut(transforms):
            register(
                f"transform:{transform.id}",
                transform.shortcuts,
                label=f'the "{transform.name}" transform',
                on_engaged=self._transform_handler(transform.id),
                on_disengaged=None,
            )

        hold_bindings, hold_dropped = _drop_duplicate_bindings(hold_bindings, kind="binding")
        tap_bindings, tap_dropped = _drop_duplicate_bindings(tap_bindings, kind="shortcut")
        dropped = {**hold_dropped, **tap_dropped}
        ledger = [
            replace(record, status="duplicate", detail=dropped[record.slot_id])
            if record.slot_id in dropped
            else record
            for record in ledger
        ]

        self._bindings.rebind(hold_bindings=hold_bindings, tap_bindings=tap_bindings)
        self._binding_ledger = ledger
        live_slot_ids = {record.slot_id for record in ledger if record.status == "registered"}
        self._binding_fires = {
            slot_id: fired for slot_id, fired in self._binding_fires.items() if slot_id in live_slot_ids
        }

        total = len(hold_bindings) + len(tap_bindings)
        for record in ledger:
            logger.info(
                "binding %s %r -> %s [%s]%s",
                record.slot_id,
                record.value,
                record.shape or "?",
                record.status,
                f" ({record.detail})" if record.detail else "",
            )
        if total:
            logger.info("bound %d binding(s)", total)
        else:
            logger.warning("no bindings are registered -- nothing will respond to the keyboard")

    def _toggle_hands_free(self, pressed_at: float = 0.0) -> None:
        """Fires on a hands-free tap -- either the dedicated key listener's
        release, or the shared chord listener's tap. Runs on a listener
        thread; must return immediately.

        ``pressed_at`` (the physical press instant BindingListener now
        passes every ``on_engaged``) is accepted and unused: hands-free is a
        toggle timed by its own DoubleTapTracker/TapModeTracker, not by how
        long a key was held, so there is no ring-buffer back-date to apply
        here the way dictation's press handler does.
        """
        tracker = self._hands_free_tracker
        if tracker is None:
            return
        outcome = tracker.tap(time.monotonic())
        if outcome is None:
            return  # a lone tap of a double-tap binding: nothing happens yet
        if outcome == "start":
            self._start_hands_free()
        else:
            self._stop_hands_free(timed_out=(outcome == "stop_timeout"))

    def _start_hands_free(self) -> None:
        self._hands_free_active = True
        # Hands-free is dictation by another gesture, so it shares the
        # dictation slot: a hands-free session and a held-key session are the
        # same intention expressed two ways and must never both dispatch.
        self._activations.begin(DICTATION)
        self._hands_free_start_frame = preroll_start_frame(self._ring.mark(), self._preroll_frames)
        self._hands_free_focused_process = self._platform.focused_process_name()
        self._capture.set_level_metering(True)
        self._start_level_pump()
        self._set_ui_state(BarState.RECORDING)
        self._hands_free_session = (
            self._start_streaming(self._hands_free_start_frame)
            if self._config.streaming.enabled
            else None
        )
        self._start_hands_free_timeout_pump()

    def _stop_hands_free(self, *, timed_out: bool) -> None:
        # The tap and the safety-timeout pump run on different threads and can
        # both decide to stop the same session; whichever gets here first owns
        # it, and the loser finds nothing outstanding and returns.
        activation = self._activations.claim(DICTATION)
        if activation is None:
            logger.debug("hands-free stop with nothing outstanding; ignoring the duplicate")
            return
        self._hands_free_active = False
        self._capture.set_level_metering(False)
        self._input_level = 0
        self._bar.set_level(0)
        session, self._hands_free_session = self._hands_free_session, None
        if timed_out:
            try:
                self._bar.coach(
                    f"Stopped automatically after {self._config.hotkey.hands_free_max_seconds}s"
                )
            except Exception:
                logger.debug("could not show the hands-free timeout coach", exc_info=True)
        self._set_ui_state(BarState.THINKING, detail="Transcribing")
        threading.Thread(
            name="flow-hands-free",
            target=self._process_hands_free,
            args=(
                self._hands_free_start_frame,
                self._hands_free_focused_process,
                session,
                timed_out,
                activation,
            ),
            daemon=True,
        ).start()

    def _start_hands_free_timeout_pump(self) -> None:
        """A session must not be able to run forever if its stop tap is
        physically missed -- checked on a timer, not just from the next tap.
        """
        if self._hands_free_timer is not None and self._hands_free_timer.is_alive():
            return

        def pump() -> None:
            while self._hands_free_active:
                tracker = self._hands_free_tracker
                if tracker is not None and tracker.check_timeout(time.monotonic()):
                    self._stop_hands_free(timed_out=True)
                    return
                time.sleep(1.0)

        self._hands_free_timer = threading.Thread(target=pump, name="flow-hands-free-timeout", daemon=True)
        self._hands_free_timer.start()

    def _start_activation_timeout_pump(self) -> None:
        """Safety net for the two *toggle-shaped* alternate chords
        (dictation's and command's -- hands-free has its own, per-session
        pump above since it is the common case). Runs for the app's whole
        lifetime rather than per-session: unlike a held key, whose release is
        guaranteed by the OS the instant the finger lifts, a tap-started
        session's stop signal can simply never arrive (a missed second tap,
        the binding edited mid-session), and CLAUDE.md is explicit that the
        app must never hang with the mic hot. A *physical hold* is never
        touched here -- only a session this engine itself marked as having
        started via a chord tap (see _dictation_started_via_chord /
        _command_started_via_chord).
        """
        if self._activation_timer is not None and self._activation_timer.is_alive():
            return

        def pump() -> None:
            while not self._stopping:
                now = time.monotonic()
                if self._dictation_started_via_chord and self._dictation_chord_tracker.check_timeout(now):
                    self._on_release()
                if self._command_started_via_chord and self._command_chord_tracker.check_timeout(now):
                    self._on_command_release()
                time.sleep(1.0)

        self._activation_timer = threading.Thread(target=pump, name="flow-activation-timeout", daemon=True)
        self._activation_timer.start()

    def _process_hands_free(
        self,
        start_frame: int,
        focused_process: str | None,
        session: ChunkStreamer | None,
        timed_out: bool,
        activation: Activation,
    ) -> None:
        with self._tracker.run({"source": "hands_free", "activation": activation.id}) as run:
            if timed_out:
                run.annotate(hands_free_timeout=True)
            self._run_pipeline(
                run,
                lambda: self._run_dictation(
                    start_frame, focused_process, session, run, activation
                ),
            )

    # -- the pipelines (run on a per-dictation/-command worker thread) ---

    def _run_pipeline(self, run: RunRecorder, body: Callable[[], None], *, label: str = "dictation") -> None:
        """Shared error handling for both the dictation and command pipelines:
        any failure marks the run and puts the overlay into a short-lived
        error state instead of hanging or crashing with the mic hot.
        """
        try:
            body()
        except GroqRateLimitError as exc:
            run.mark_failed(exc)
            logger.warning("%s", exc)
            # Name the limit that was actually hit rather than a bare "rate
            # limited": on Groq's free tier the daily audio budget and the
            # per-minute request budget need completely different responses
            # from the user (wait a minute vs. stop for the day).
            detail = "rate limited"
            if exc.limit_type:
                detail += f" ({exc.limit_type})"
            if exc.retry_after_s is not None:
                detail += f" — retry in {exc.retry_after_s:.0f}s"
            self._record_error(
                kind="rate_limited",
                headline=f"Rate limited — {exc.limit_type}" if exc.limit_type else "Rate limited",
                detail="This resolves on its own. Nothing to do.",
                actionable=False,
                retry_after_s=exc.retry_after_s,
            )
            self._set_ui_state(BarState.ERROR, detail=detail, kind="rate_limited")
        except TimeoutError as exc:
            run.mark_failed(exc)
            logger.error("%s timed out: %s", label, exc)
            self._record_error(
                kind="timeout",
                headline="Groq did not respond in time",
                detail="Retrying next time. Check your connection if it keeps happening.",
                actionable=False,
            )
            self._set_ui_state(BarState.ERROR, detail="timed out", kind="timeout")
        except (GroqError, SwayError) as exc:
            run.mark_failed(exc)
            logger.error("%s failed: %s", label, exc)
            described = _describe_failure(exc)
            self._record_error(**described)
            self._set_ui_state(
                BarState.ERROR, detail=_short_failure(exc), kind=described["kind"]
            )
        except Exception as exc:  # never let a bug hang the app with the mic hot
            run.mark_failed(exc)
            logger.exception("unexpected %s failure", label)
            self._record_error(
                kind="unexpected",
                headline="Something went wrong",
                detail="The details are in Sway's log file.",
                actionable=False,
            )
            self._set_ui_state(BarState.ERROR, detail="failed", kind="unexpected")

    def _process(
        self,
        start_frame: int,
        focused_process: str | None,
        session: ChunkStreamer | None,
        release_time: float,
        held_ms: float,
        press_time: float,
        activation: Activation,
    ) -> None:
        with self._tracker.run({"source": "hotkey", "activation": activation.id}) as run:
            run.annotate(held_ms=round(held_ms, 1))
            # Two spans that closed before this recorder existed (D0). The
            # hold is the user's own speaking time and is not latency to
            # optimise, but without it the report cannot relate clip length
            # to anything. Dispatch -- release to this thread being scheduled
            # -- is pure overhead, and was previously invisible: the tracker's
            # TOTAL starts here, so it never counted the gap it sits after.
            run.record(
                Stage.HOTKEY_HOLD,
                held_ms,
                # started_at is wall clock, but press_time is monotonic;
                # convert by measuring back from now rather than calling
                # time.time() at press on the hot listener thread.
                started_at=time.time() - (time.monotonic() - press_time),
            )
            run.record(Stage.DISPATCH, (time.monotonic() - release_time) * 1000.0)
            self._run_pipeline(
                run,
                lambda: self._run_dictation(
                    start_frame, focused_process, session, run, activation
                ),
            )
            # The number that actually describes the wait: hotkey release to
            # text on screen. Every other stage timing is a component of it,
            # and the tracker's own TOTAL starts when this worker thread was
            # scheduled, which is fractionally later.
            run.annotate(release_to_paste_ms=round((time.monotonic() - release_time) * 1000.0, 1))

    def _run_dictation(
        self,
        start_frame: int,
        focused_process: str | None,
        session: ChunkStreamer | None,
        run: RunRecorder,
        activation: Activation,
    ) -> None:
        transcript = self._transcribe(start_frame, run, session)
        if not transcript:
            # Previously a silent return to idle, which is indistinguishable
            # from the app having ignored the user entirely -- and it is the
            # visible symptom of a muted or misrouted microphone. Naming it,
            # and offering the device picker, turns the most confusing
            # outcome in the product into the most actionable one.
            self._set_ui_state(
                BarState.DONE, detail="Not captured — no speech detected", kind="no_speech"
            )
            return

        hallucinated = looks_hallucinated(transcript.text, speech_ms=transcript.speech_ms)
        if not transcript.confident or hallucinated:
            # VAD is an energy classifier, not a content classifier: a
            # keyboard click or a breath can survive it as a short non-empty
            # clip, and Whisper is prone to hallucinate by echoing back the
            # personal-dictionary prompt bias (see _build_prompt_bias), or
            # its own famous training-data boilerplate ("Thank you for
            # watching!"), over audio like that. Two independent signals
            # catch this: transcript.confident (Whisper's own avg_logprob/
            # no_speech_prob) and looks_hallucinated (the audio was too
            # short to physically contain this many words) -- the second
            # exists because the classic hallucinated phrases are often
            # reported *confidently*, so the model's own uncertainty signal
            # alone does not reliably catch them. Either one is enough to
            # withhold the paste: named clearly on the bar rather than
            # silently pasted or left ambiguous, and never handed to
            # triggers, corrections, or a cleanup model call, none of which
            # should do real work over text that is likely not what was
            # actually said.
            run.annotate(
                skipped="hallucination_suspected" if hallucinated else "low_confidence_paste_suppressed"
            )
            self._set_ui_state(
                BarState.DONE, detail="Not captured — didn't catch that", kind="low_confidence"
            )
            return

        # Local, zero-cost substitutions happen before the model sees the
        # text, so a pasted signature or address is never "tidied up".
        triggered, transforms = self._apply_triggers(transcript.text, run)
        if triggered != transcript.text:
            transcript = replace(transcript, text=triggered)

        # Local, pure, sub-millisecond. Runs after triggers so a substituted
        # macro cannot be mistaken for a retraction, and before cleanup
        # because its result decides whether cleanup runs at all.
        correction = self._scan_corrections(transcript.text)

        cleaned_text = self._clean_up(transcript, run, focused_process, correction, transforms)

        self._set_ui_state(BarState.INJECTING)
        with run.stage(Stage.INJECT):
            pasted = self._inject_with_fallback(cleaned_text, run, activation)

        if pasted:
            # Confirm with a fact, not a tick: the word count also silently
            # answers "did anything actually land?", which is the failure the
            # user is most likely to be watching for.
            words = count_words(cleaned_text)
            self._success_count += 1
            self._tap_streak = 0
            self._remember_transcript(cleaned_text)
            self._set_ui_state(
                BarState.DONE, detail=f"{words} word{'' if words == 1 else 's'}"
            )
            self._refresh_bar_hint()
            self._coach_amend_gesture()
            # A success clears the "what is wrong right now" slot, so Home
            # never shows a stale failure the user has already worked past.
            self._clear_error()
        else:
            self._set_ui_state(
                BarState.ERROR,
                detail="elevated window — copied instead",
                kind="paste_blocked",
            )

        # Everything below runs *after* the text is on screen, so none of it
        # counts against the sub-second budget CLAUDE.md sets.
        metrics = self._session_metrics(cleaned_text, transcript, start_frame)
        run.annotate(
            words=metrics.word_count,
            wpm=round(metrics.wpm, 1) if metrics.wpm is not None else "",
            time_saved_s=round(metrics.time_saved_s, 1),
        )
        self._update_vocabulary(cleaned_text, transcript)
        self._record_history(
            kind=KIND_DICTATION,
            text=cleaned_text,
            raw_text=transcript.text,
            focused_process=focused_process,
            run_id=run.id,
            metrics=metrics,
            language=transcript.language,
            status="pasted" if pasted else "clipboard_fallback",
        )

    def _session_metrics(
        self, text: str, transcript: _Transcript, start_frame: int
    ) -> SessionMetrics:
        """Derive this session's word count, WPM, and time-saved estimate.

        On the streaming path (Phase 4) the transcript carries no measured
        durations, because no single contiguous clip was ever VAD-trimmed.
        Rather than report no speaking rate for exactly the long dictations
        where it matters most, the clip is measured here -- after the paste
        has already landed, so the extra VAD pass costs the user nothing.
        A measurement failure yields ``None`` durations and therefore no WPM,
        which the dashboard renders as "not available" rather than as zero.
        """
        speech_ms, audio_ms = transcript.speech_ms, transcript.audio_ms
        if speech_ms is None:
            try:
                pcm = self._ring.read_since(start_frame)
                trimmed = self._vad.trim(pcm)
                rate, frame_size = self._config.audio.sample_rate, self._ring.frame_size
                audio_ms = pcm_duration_ms(len(pcm), rate, frame_size=frame_size)
                speech_ms = pcm_duration_ms(len(trimmed), rate, frame_size=frame_size)
            except Exception:  # telemetry must never break a working dictation
                logger.debug("could not measure speech duration for metrics", exc_info=True)
                speech_ms, audio_ms = 0.0, 0.0
        return session_metrics(
            text,
            speech_ms=speech_ms or 0.0,
            audio_ms=audio_ms or 0.0,
            typing_wpm=self._config.analytics.typing_wpm,
        )

    def _inject_with_fallback(
        self, text: str, run: RunRecorder, activation: Activation | None = None
    ) -> bool:
        """Paste ``text``, or -- when the focused window is confidently
        elevated (:meth:`~flow.platform.PlatformAdapter.synthetic_input_likely_blocked`)
        and a synthetic paste would silently do nothing -- leave it on the
        clipboard instead so the user can paste it themselves with a real
        keystroke, which UIPI does not block. Returns whether the paste
        itself succeeded (``False`` on the clipboard-only fallback).
        """
        # The last line of defence in the one-intention-one-execution chain.
        # Whatever produced a second run of this pipeline -- a duplicate
        # release, a retry, a re-entered worker -- only the first holder of
        # this activation may put text on screen.
        if activation is not None and not self._activations.mark_injected(activation):
            logger.warning(
                "refusing a second paste for activation %s (this is the duplicate "
                "that would have doubled the text)",
                activation,
            )
            run.annotate(duplicate_injection_suppressed=True)
            return False
        try:
            self._injector.inject(text)
            return True
        except PasteBlockedError:
            run.annotate(paste_blocked=True)
            logger.warning(
                "the focused window appears to be elevated; left the cleaned-up text on "
                "the clipboard instead of pasting it"
            )
            try:
                self._platform.clipboard_set(text)
            except SwayError:
                logger.warning("could not leave the cleaned-up text on the clipboard either")
            return False

    def _transcribe(
        self, start_frame: int, run: RunRecorder, session: ChunkStreamer | None
    ) -> _Transcript | None:
        """The raw (pre-cleanup) transcript, or ``None`` if there's nothing to
        say (empty capture / no detected speech anywhere in the hold).

        If a streaming session ran and produced at least one chunk cleanly,
        only the short tail since the last chunk needs a fresh Groq request
        (Phase 4). If the session never got far enough to chunk anything
        (a short hold) or hit any failure along the way, this transparently
        falls back to :meth:`_transcribe_single_shot` — one request over the
        whole clip, exactly Phase 1's behavior, so a network hiccup during
        streaming never loses a word of the dictation.
        """
        if session is not None:
            result = session.stop(timeout=self._config.groq.timeout_s + 5.0)
            run.annotate(streaming_chunks=result.chunk_count, streaming_degraded=result.degraded)
            if result.chunk_count > 0 and not result.degraded:
                try:
                    return self._transcribe_streamed_tail(run, result)
                except (GroqError, TimeoutError) as exc:
                    logger.warning(
                        "streaming tail transcription failed (%s); falling back to a single "
                        "whole-clip request",
                        exc,
                    )
                    run.annotate(streaming_fallback=True, streaming_fallback_reason=str(exc))
        return self._transcribe_single_shot(start_frame, run)

    def _transcribe_single_shot(self, start_frame: int, run: RunRecorder) -> _Transcript | None:
        """Phase 1's pipeline: one Groq request over the whole captured clip.
        Also used, unmodified, to transcribe a voice command's short spoken
        instruction (Phase 5) -- both are "the whole clip since some start
        frame," just with different callers and start points.
        """
        with run.stage(Stage.CAPTURE):
            pcm = self._ring.read_since(start_frame)
        if not pcm:
            run.annotate(skipped="empty_capture")
            return None

        with run.stage(Stage.VAD):
            trimmed = self._vad.trim(pcm)
        if not trimmed:
            run.annotate(skipped="no_speech")
            return None

        sample_rate = self._config.audio.sample_rate
        frame_size = self._ring.frame_size
        audio_ms = pcm_duration_ms(len(pcm), sample_rate, frame_size=frame_size)
        speech_ms = pcm_duration_ms(len(trimmed), sample_rate, frame_size=frame_size)

        with run.stage(Stage.ENCODE):
            flac_bytes = encode_flac(
                trimmed, self._config.audio.sample_rate, self._config.audio.channels
            )

        prompt_bias = self._build_prompt_bias()
        prior = self._language_prior()
        stt_model = self._stt_model_for(prior)
        with run.stage(Stage.TRANSCRIBE):
            self._require_groq()
            # Opened here and closed from the network thread when Groq's
            # response headers land, splitting the transcribe stage into
            # "waiting for Groq to say anything" and "reading the body".
            # If the request fails before any response, the span is simply
            # never closed and therefore never recorded -- not measured,
            # rather than recorded as the full timeout.
            first_byte = run.begin(Stage.STT_FIRST_BYTE)
            future = self._network.submit(
                self._groq.transcribe(
                    flac_bytes,
                    language=self._config.groq.language,
                    prompt=prompt_bias or None,
                    model=stt_model,
                    on_first_byte=lambda: run.end(first_byte),
                )
            )
            result = future.result(timeout=self._config.groq.timeout_s + 5.0)
            retried = self._retry_on_accurate_stt(
                result,
                flac_bytes,
                prompt_bias=prompt_bias,
                used_model=stt_model,
                speech_ms=speech_ms,
            )
            if retried is not None:
                result, stt_model = retried, self._config.groq.stt_accurate_model
                run.annotate(stt_retried=True)

        run.annotate(
            characters=len(result.text),
            language=result.language or "",
            stt_model=stt_model,
            avg_logprob=result.avg_logprob if result.avg_logprob is not None else "",
            confident=result.confident,
            audio_ms=round(audio_ms, 1),
            speech_ms=round(speech_ms, 1),
        )
        if not result.text:
            return None
        return _Transcript(
            text=result.text,
            language=result.language,
            confident=result.confident,
            speech_ms=speech_ms,
            audio_ms=audio_ms,
        )

    def _transcribe_streamed_tail(self, run: RunRecorder, result: ChunkSessionResult) -> _Transcript | None:
        """The Phase 4 fast path: transcribe only the tail, then stitch it
        onto the chunk texts already collected while the user was speaking.
        """
        now = self._ring.mark()
        tail_start, _tail_end = result.clock.tail_range(now)

        with run.stage(Stage.CAPTURE):
            tail_pcm = self._ring.read_since(tail_start)
        with run.stage(Stage.VAD):
            tail_trimmed = self._vad.trim(tail_pcm) if tail_pcm else b""

        tail_text = ""
        tail_language: str | None = None
        tail_confident = True
        if tail_trimmed:
            with run.stage(Stage.ENCODE):
                flac_bytes = encode_flac(
                    tail_trimmed, self._config.audio.sample_rate, self._config.audio.channels
                )
            prompt_bias = self._build_prompt_bias()
            with run.stage(Stage.TRANSCRIBE):
                self._require_groq()
                future = self._network.submit(
                    self._groq.transcribe(
                        flac_bytes,
                        language=self._config.groq.language,
                        prompt=prompt_bias or None,
                        model=self._stt_model_for(self._language_prior()),
                    )
                )
                tail_result = future.result(timeout=self._config.groq.timeout_s + 5.0)
                tail_text = tail_result.text
                tail_language = tail_result.language
                tail_confident = tail_result.confident

        with run.stage(Stage.MERGE):
            merged = stitch_transcripts(
                [*result.chunk_texts, tail_text],
                max_overlap_words=max_overlap_words(self._config.streaming.overlap_seconds),
            )

        # The tail's detected language stands in for the whole utterance: it's
        # the only piece language-labeled at this point (chunk_texts are
        # plain strings by the time they reach here), and a dictation rarely
        # switches language mid-utterance.
        run.annotate(
            streaming=True,
            streaming_chunk_texts=len(result.chunk_texts),
            streaming_tail_chars=len(tail_text),
            characters=len(merged),
            language=tail_language or "",
        )
        # Empty is possible (every chunk and the tail alike found no speech,
        # e.g. a hold that was all silence past the first chunk): match
        # _transcribe_single_shot's contract of None rather than an empty
        # _Transcript, which the caller's `if not transcript` check relies on.
        if not merged:
            return None
        # Known gap: `confident` here is only the *tail* chunk's confidence.
        # Earlier chunks (result.chunk_texts) were already accepted into the
        # merge while the user was still speaking, with no per-chunk
        # confidence gate of their own -- so the paste-gate in _run_dictation
        # reliably protects the whole-clip path but not a hallucinated
        # *earlier* chunk on a long streamed dictation. Retrofitting
        # per-chunk gating into ChunkStreamer is a larger Phase 4 change and
        # out of scope here.
        return _Transcript(text=merged, language=tail_language, confident=tail_confident)

    def _retry_on_accurate_stt(
        self,
        result: TranscriptionResult,
        flac_bytes: bytes,
        *,
        prompt_bias: str,
        used_model: str,
        speech_ms: float,
    ) -> TranscriptionResult | None:
        """Re-transcribe on the full model when turbo looks out of its depth.

        Fires only when turbo has just told us something we could not know
        beforehand: that the speech is in a language it decodes badly. On Auto
        that is the *only* signal available, and without this a Marathi
        speaker who has not set their language gets a corrupted transcript
        with nothing in the system aware of it.

        Bounded deliberately. Long clips are excluded because a second full
        transcription costs more than the error it fixes and would double the
        audio spend against a daily quota. Returns ``None`` to keep the
        original result, and never raises -- a failed retry degrades to the
        transcript we already have rather than losing the user's words.
        """
        if used_model == self._config.groq.stt_accurate_model:
            return None
        # An explicit setting already picked the model; respect it.
        if languages.normalize(self._config.groq.language):
            return None
        if speech_ms > _STT_RETRY_MAX_SPEECH_MS:
            return None

        detected = languages.get(result.language)
        if detected is not None and detected.needs_accurate_stt:
            # A language we know turbo decodes badly.
            why, hint = f"{detected.code} decodes poorly on turbo", detected.code
        elif result.language and detected is None:
            # Whisper named a language Sway has no profile for at all. On a
            # short Indic clip this is exactly what failure looks like: one
            # speaker's Marathi came back as english, then indonesian, then
            # arabic, then urdu, each reported confidently. Treat an
            # unsupported label as evidence the detection is junk rather than
            # as a fact, and let the full model have a go -- its language
            # identification is markedly better than turbo's.
            why, hint = f"{result.language!r} is not a language Sway supports", (
                self._session_language()
            )
        elif not result.confident:
            why, hint = "the decode was low-confidence", self._session_language()
        else:
            return None

        logger.info(
            "re-transcribing on %s: %s", self._config.groq.stt_accurate_model, why
        )
        try:
            future = self._network.submit(
                self._groq.transcribe(
                    flac_bytes,
                    language=hint,
                    prompt=prompt_bias or None,
                    model=self._config.groq.stt_accurate_model,
                )
            )
            retried = future.result(timeout=self._config.groq.timeout_s + 5.0)
        except Exception:
            logger.warning(
                "accurate re-transcribe failed; keeping the first result", exc_info=True
            )
            return None
        return retried if retried.text.strip() else None

    # -- language resolution ---------------------------------------------

    def _session_language(self) -> str | None:
        """The language of recent dictations, when they agree enough to be a
        useful prior. ``None`` when there is no history or it is mixed."""
        with self._language_lock:
            recent = list(self._language_history)
        if not recent:
            return None
        # The most recent verdict wins ties: a genuine switch should take
        # effect on the next utterance, not after three.
        return max(set(recent), key=lambda code: (recent.count(code), recent.index(code)))

    def _remember_language(self, code: str | None) -> None:
        """Record a confident language verdict for later utterances."""
        normalized = languages.normalize(code)
        if not normalized:
            return
        with self._language_lock:
            self._language_history.append(normalized)

    def _language_prior(self) -> str | None:
        """The best guess at the language *before* any audio is sent.

        Explicit beats remembered beats nothing. This is what picks the STT
        model, so it can only use what is known ahead of the request -- the
        transcript's own evidence arrives too late for that decision.
        """
        explicit = languages.normalize(self._config.groq.language)
        if explicit:
            return explicit
        return self._session_language()

    def _stt_model_for(self, code: str | None) -> str:
        """Which Whisper to send this dictation to.

        Turbo unless the language is one whose transcription measurably
        degrades on the distilled decoder, in which case the full model earns
        its extra ~140ms (see sway.languages.Language.needs_accurate_stt).
        """
        language = languages.get(code)
        if language is not None and language.needs_accurate_stt:
            return self._config.groq.stt_accurate_model
        return self._config.groq.stt_model

    def _streaming_wanted(self) -> bool:
        """Whether to ship overlapping chunks while the user is still talking.

        Off for languages that need the accurate decoder. Chunking splits
        speech at arbitrary boundaries, and a decoder already bending Marathi
        toward Hindi on a whole clip does it worse on a fragment with no
        surrounding context -- then the errors are stitched together and
        cannot be told apart. Falling back to the single-shot path costs the
        streaming head start and buys a transcript that is right.
        """
        if not self._config.streaming.enabled:
            return False
        language = languages.get(self._language_prior())
        return language is None or not language.needs_accurate_stt

    def _language_for_transcript(self, transcript: _Transcript) -> str | None:
        """The language to treat a finished transcript as being in.

        An explicit setting is authoritative. Failing that, Whisper's label is
        trusted only where it has been shown to be trustworthy:

        * Under about two seconds of speech it is close to random, so the
          session's recent language replaces it entirely.
        * Between two languages that share a script it cannot discriminate at
          all -- it reports every Marathi utterance as Hindi -- so a session
          prior in the same script wins. Across scripts it is reliable and
          wins, which is what lets a genuine switch take effect.
        """
        explicit = languages.normalize(self._config.groq.language)
        if explicit:
            return explicit
        detected = languages.normalize(transcript.language)
        session = self._session_language()
        speech_ms = transcript.speech_ms
        if speech_ms is not None and speech_ms < _LANGUAGE_MIN_SPEECH_MS:
            return session or detected
        if detected and session and detected != session:
            # Same script means Whisper was choosing between two options it
            # cannot tell apart; what the last few dictations actually were is
            # the better evidence.
            if languages.scripts_for(detected) & languages.scripts_for(session):
                return session
        return detected or session

    def _target_language(self) -> str | None:
        """The language the pasted text should end up in, or ``None`` for
        "whatever was spoken" -- the default, and not a translation.

        Read fresh from settings on every dictation, so the bar's popover and
        the dashboard both take effect on the very next utterance.
        """
        return languages.normalize(self._settings.get().target_language)

    def _build_prompt_bias(self) -> str:
        """The personal dictionary, joined for Whisper's ``prompt`` parameter.

        Reads the store fresh on every dictation rather than caching in
        memory: a SQLite read of a table this small is sub-millisecond, far
        below the transcription network call it precedes, and it means
        `flow dictionary add` takes effect on the very next dictation instead
        of requiring a restart.
        """
        if self._dictionary is None:
            return ""
        try:
            entries = self._dictionary.list_entries()
        except DictionaryStoreError as exc:
            logger.warning("could not read the personal dictionary: %s", exc)
            return ""
        # Ordered before it is truncated. The budget is a couple of hundred
        # characters, so with a real dictionary the order decides which terms
        # get biased at all -- alphabetical would mean "whichever start with A".
        return build_prompt_bias(
            prioritise(entries), max_chars=self._config.dictionary.max_prompt_chars
        )

    def _clean_up(
        self,
        transcript: _Transcript,
        run: RunRecorder,
        focused_process: str | None,
        correction: CorrectionScan | None = None,
        transforms: Sequence[Transform] = (),
    ) -> str:
        """Runs the cleanup pass over ``transcript`` in the style of the
        focused app's profile (Phase 3), or bypasses it per the skip
        heuristic / ``[cleanup].enabled`` / a VERBATIM profile (terminals,
        code editors). Raises the same ``GroqError``/``GroqRateLimitError``/
        ``TimeoutError`` family as transcription, handled by the caller's
        error branches.

        The skip heuristic uses ``transcript.language`` (Whisper's detected
        language) to pick a language-appropriate filler-word list -- see
        :func:`~flow.cleanup.should_skip_cleanup`. The LLM's output is also
        checked by :func:`~flow.cleanup.is_cleanup_output_safe`: a rewrite
        that grew, shrank, or emptied itself far more than a tidy-up ever
        should falls back to the raw transcript rather than risk pasting a
        response the model generated instead of a rewrite.
        """
        cfg = self._config.cleanup
        style = self._style_for(focused_process)
        raw_text = transcript.text
        guardrail = ""
        failure = ""
        # The skip decision is measured *outside* the CLEANUP span, and the
        # span is opened only when the model is actually called (D0).
        #
        # It used to wrap both branches, which recorded a ~0.00 ms CLEANUP
        # span on every skipped utterance. Those zeros then sat in the same
        # percentile pool as real round trips and halved the reported p50 --
        # a stage that costs 238 ms when it runs read as 178 ms, and got
        # cheaper the *better* the skip heuristic worked. Any D3 before/after
        # comparison built on that would have been meaningless, since
        # improving the skip rate moves the cleanup percentile without the
        # model call getting any faster. Skips are already recorded as
        # `cleanup_skipped` / `cleanup_skip_reason` metadata, which is where
        # skip-rate analysis belongs.
        if correction is None:
            correction = CorrectionScan(text=raw_text)
        # Resolved once and threaded through, so the skip decision, the
        # prompt, the model choice, and the guardrail all reason about the
        # same language rather than each re-deriving it.
        language = self._language_for_transcript(transcript)
        target_language = self._target_language()
        if language is None and transcript.language:
            # The app's Logs view shows warnings and errors only, so an
            # unresolvable language used to leave no trace anywhere: the user
            # got confident-looking text in a script they cannot read and
            # nothing in the product admitted anything had gone wrong.
            logger.warning(
                "this dictation was detected as %r, which Sway has no profile for, "
                "so it was pasted exactly as transcribed. Auto-detect is unreliable "
                "on short clips -- if that language is wrong, pick yours from the "
                "globe on the bar.",
                transcript.language,
            )
        skip_reason = self._cleanup_skip_reason(
            transcript,
            style,
            correction,
            transforms,
            language=language,
            target_language=target_language,
        )
        if skip_reason:
            cleaned_text = raw_text
        else:
            try:
                with run.stage(Stage.CLEANUP):
                    cleaned_text, guardrail = self._request_cleanup(
                        raw_text,
                        style,
                        transcript,
                        run,
                        correction,
                        transforms,
                        language=language,
                        target_language=target_language,
                    )
            except (GroqError, TimeoutError) as exc:
                # The transcript is the user's words and they are already
                # captured; a cleanup outage must cost them the tidy-up,
                # never the dictation. This is deliberately caught here
                # rather than in _run_pipeline, which would abandon the
                # run and paste nothing at all.
                failure = _failure_label(exc)
                cleaned_text = raw_text
                logger.warning("cleanup unavailable (%s); pasting the raw transcript", exc)
        skipped = bool(skip_reason)
        self._cleanup_stats.record(skipped)
        run.annotate(
            cleanup_skipped=skipped,
            cleanup_skip_reason=skip_reason,
            cleaned_characters=len(cleaned_text),
            cleanup_style=str(style),
            cleanup_guardrail_tripped=bool(guardrail),
            cleanup_guardrail_reason=guardrail,
            cleanup_failure=failure,
            focused_process=focused_process or "",
            language=transcript.language or "",
            resolved_language=language or "",
            target_language=target_language or "",
            transcript_confident=transcript.confident,
            correction_detected=correction.detected,
            correction_markers=correction.describe(),
            correction_kinds=",".join(correction.kinds),
        )
        return cleaned_text

    def _update_vocabulary(self, text: str, transcript: _Transcript) -> None:
        """Count which dictionary terms were used, and learn new ones.

        Runs *after* the paste, on the same post-injection block as the history
        write, so none of it counts against the sub-second budget. Never
        raises: vocabulary bookkeeping must not be able to fail a dictation
        that has already succeeded.

        Learning is gated on ``transcript.confident``. A word Whisper itself
        was unsure of is the last thing that should be fed back in as a
        transcription bias -- that is how a mishearing becomes permanent.
        """
        if self._dictionary is None or not text.strip():
            return
        try:
            entries = self._dictionary.list_entries()
            known = [entry.term for entry in entries]
            used = [term for term in known if _mentions(text, term)]
            if used:
                self._dictionary.record_usage(used)

            if not self._settings.get().auto_learn_vocabulary:
                return
            if not transcript.confident:
                return
            learned = self._dictionary.observe_candidates(candidate_terms(text, known=known))
            if learned:
                logger.info("learned %d new vocabulary term(s): %s", len(learned), ", ".join(learned))
        except Exception:
            logger.exception("could not update the personal dictionary")

    def _scan_corrections(self, text: str) -> CorrectionScan:
        """Look for spoken self-corrections. Never raises.

        A bug in detection must cost the user their tidy-up at worst, never
        their words, so any failure degrades to "no correction found" -- which
        is exactly the behaviour this build had before the scan existed.
        """
        try:
            return scan_corrections(text, mode=self._settings.get().context_mode)
        except Exception:
            logger.exception("correction scan failed; treating the transcript as-spoken")
            return CorrectionScan(text=text)

    def _cleanup_skip_reason(
        self,
        transcript: _Transcript,
        style: CleanupStyle,
        correction: CorrectionScan,
        transforms: Sequence[Transform] = (),
        language: str | None = None,
        target_language: str | None = None,
    ) -> str:
        """Why this transcript bypasses the LLM, or ``""`` to run cleanup.

        A detected self-correction overrides the *heuristic* skips, because
        they are the ones that get it wrong: "meeting at 7, no, 8" is five
        words with no filler, so ``short_and_clean`` would paste the retracted
        time, and a speaker who stumbles is exactly the speaker Whisper is
        least confident about. It does not override ``disabled`` (the user
        turned cleanup off) or ``verbatim_profile`` (a terminal or code
        editor, where the text must reach the cursor exactly as transcribed --
        a correction is not worth rewriting someone's shell command over).
        """
        cfg = self._config.cleanup
        if not cfg.enabled:
            return "disabled"
        if style is CleanupStyle.VERBATIM:
            return "verbatim_profile"
        # Translation is held to a stricter bar than cleanup, and this check
        # sits above the correction/transform override on purpose. That
        # override exists because a stumbling speaker is exactly who a
        # low-confidence tidy-up helps most -- but rewriting shaky words in
        # the same language is recoverable, and rendering them into another
        # language the user cannot read back is not. When the transcript
        # cannot be trusted, the raw words are pasted untouched.
        if target_language and not transcript.confident:
            return "language_uncertain"
        # A fired transform has already had its phrase excised, so skipping the
        # call now would drop the words and deliver nothing for them.
        if transforms or correction.detected:
            return ""
        # An explicit output language must survive the heuristic skips: a
        # short, clean utterance still has to be translated, and
        # "short_and_clean" would otherwise paste it in the spoken language.
        if target_language:
            return ""
        if cfg.skip_when_unconfident and not transcript.confident:
            return "low_confidence"
        if should_skip_cleanup(
            transcript.text, language=language, min_words=cfg.skip_min_words
        ):
            return "short_and_clean"
        return ""

    def _cleanup_model_for(self, language: languages.Language | None) -> str:
        """Which chat model rewrites this text.

        Per-language routing exists because model quality in a language is
        not uniform: the default is measurably stiffer in Hindi and Marathi,
        producing textbook constructions where a person would keep the
        English word. A language with no override uses the configured model,
        so English is untouched.
        """
        if language is not None and language.cleanup_model:
            return language.cleanup_model
        return self._config.cleanup.model

    def _request_cleanup(
        self,
        raw_text: str,
        style: CleanupStyle,
        transcript: _Transcript,
        run: RunRecorder,
        correction: CorrectionScan,
        transforms: Sequence[Transform] = (),
        language: str | None = None,
        target_language: str | None = None,
    ) -> tuple[str, str]:
        """The cleanup round trip plus its output checks. Returns the text to
        paste and the guardrail reason that rejected the rewrite (``""`` when
        the rewrite was accepted).
        """
        cfg = self._config.cleanup
        self._require_groq()
        # Time to first *content* token. With reasoning_effort set, a large
        # share of this stage is the model thinking before it emits anything,
        # so the split says whether D3 should chase the model's latency or the
        # length of what it generates.
        first_token = run.begin(Stage.LLM_FIRST_TOKEN)
        spoken = language if language is not None else languages.normalize(transcript.language)
        spoken_language = languages.get(spoken)
        target = languages.get(target_language)
        # The register hint belongs to whichever language is being *written*:
        # translating into Marathi needs Marathi's register, not the register
        # of the language it came from.
        register_source = target or spoken_language
        # Ask the model what language it is actually looking at whenever that
        # is in doubt. Whisper cannot tell Hindi from Marathi and is close to
        # random on short clips; the model reading the text gets both right,
        # and it is already being called. Deliberately not requested on the
        # plain-English path, which keeps that prompt byte-identical to the
        # one this build has always sent.
        emit_header = spoken != "en"
        future = self._network.submit(
            self._groq.cleanup(
                raw_text,
                system_prompt=system_prompt_for_style(
                    style,
                    language=transcript.language,
                    correction=correction.detected,
                    remove_fillers=self._settings.get().remove_fillers,
                    register=register_source.register_hint if register_source else "",
                    target_language=target.code if target else None,
                    emit_language_header=emit_header,
                )
                # Appended last, after the byte-identical style prompt, so the
                # stable prefix stays cacheable (D8.3).
                + build_directive_block(transforms),
                model=self._cleanup_model_for(register_source),
                temperature=cfg.temperature,
                reasoning_effort=cfg.reasoning_effort or None,
                max_output_tokens=cfg.max_output_tokens or None,
                on_first_token=lambda: run.end(first_token),
            )
        )
        result = future.result(timeout=self._config.groq.timeout_s + 5.0)
        body = result.text
        if emit_header:
            verdict, body = parse_language_header(result.text)
            if verdict.code:
                run.annotate(detected_language=verdict.code, mixed_language=verdict.mixed or "")
                if verdict.confident:
                    # Improves the *next* dictation, not this one: the verdict
                    # arrives after the transcript was already decoded.
                    self._remember_language(verdict.code)
        candidate = normalize_cleanup_output(body, raw_text)
        if not candidate:
            return raw_text, "empty"
        # The entity guard treats every number as protected, so a *correct*
        # rewrite of "at 7am, no no, at 8am" looks exactly like the model
        # having lost the 7. Handing it the retracted span is what tells the
        # two apart -- and only the retracted span, so every other figure in
        # the utterance stays protected.
        retracted = correction.retracted_text()
        # What the fired transforms asked the model to do, so the guardrails
        # can tell "it did what the user asked" apart from "it went
        # off-script". Without this a correct bulleted rewrite is rejected by
        # the markdown check and the user silently gets unbulleted text.
        # Empty when nothing fired, which leaves plain dictation's guardrails
        # exactly as they were.
        expectation = expectation_for(transforms)
        # What script change the user actually asked for. Empty unless a
        # target language is set, which leaves the guardrail exactly as it
        # has always been for everyone else.
        intent = language_intent_for(source=spoken, target=target.code if target else None)
        reason = unsafe_reason(
            raw_text,
            candidate,
            retracted=retracted,
            correction_detected=correction.detected,
            expectation=expectation,
            intent=intent,
        )
        if reason:
            logger.warning(
                "cleanup output rejected by the %s guardrail (raw %d words -> cleaned %d words); "
                "pasting the raw transcript instead",
                reason,
                len(raw_text.split()),
                len(candidate.split()),
            )
            return raw_text, reason
        return candidate, ""

    # -- the voice-command pipeline (Phase 5) -----------------------------

    def _process_command(
        self,
        start_frame: int,
        focused_process: str | None,
        selection_ready: threading.Event,
        instruction: str = "",
        source: str = "command",
        expectation: OutputExpectation = SPOKEN_COMMAND_EXPECTATION,
        activation: Activation | None = None,
        *,
        transform: Transform | None = None,
    ) -> None:
        metadata: dict[str, Any] = {"source": source}
        if activation is not None:
            metadata["activation"] = activation.id
        with self._tracker.run(metadata) as run:
            self._run_pipeline(
                run,
                lambda: self._run_command(
                    start_frame,
                    focused_process,
                    selection_ready,
                    run,
                    instruction,
                    expectation,
                    activation,
                    transform=transform,
                ),
                label="command",
            )

    def _run_command(
        self,
        start_frame: int,
        focused_process: str | None,
        selection_ready: threading.Event,
        run: RunRecorder,
        instruction: str = "",
        expectation: OutputExpectation = SPOKEN_COMMAND_EXPECTATION,
        activation: Activation | None = None,
        *,
        transform: Transform | None = None,
    ) -> None:
        budget_s = selection_budget_s(self._config.commands.selection_timeout_s)
        with run.stage(Stage.SELECTION):
            finished = selection_ready.wait(timeout=budget_s)
            selected_text = self._selected_text
        if not selected_text:
            run.annotate(skipped="empty_selection", selection_timed_out=not finished)
            label = f"transform {transform.name}" if transform is not None else "voice command"
            # This failure previously wrote to the latency DB and nowhere
            # else, which is why a broken synthetic Ctrl+C (see
            # sway.selection's modifier wait) went undiagnosable for as long
            # as it did. The two causes below are unrelated -- one means the
            # capture thread never got a chance to finish, the other means
            # it finished and genuinely found nothing -- so distinguish them
            # rather than folding both into one line.
            if not finished:
                logger.warning(
                    "%s: the selection read did not finish within %.2fs; treating it as empty",
                    label,
                    budget_s,
                )
            else:
                logger.warning(
                    "%s: no text selection to work on -- the copy shortcut returned "
                    "nothing (see the copy-attempt log lines above)",
                    label,
                )
            # Say what to do about it, not just what went wrong: "nothing
            # selected" alone left the user guessing whether the shortcut
            # had even registered.
            self._set_ui_state(
                BarState.ERROR,
                detail="Select some text first, then press the shortcut",
                kind="empty_selection",
            )
            return

        # The selection is in hand and the network call is next -- the slow
        # part. Retitle so the bar reads as progressing rather than stuck on
        # whatever it said a second ago.
        if transform is not None:
            self._set_ui_state(BarState.THINKING, detail=f"Rewriting — {transform.name}")

        # A transform supplies its instruction from storage, so there is
        # nothing to transcribe and this path makes one network call where a
        # spoken command makes two.
        if instruction:
            instruction_text = instruction
        else:
            spoken = self._transcribe_single_shot(start_frame, run)
            if not spoken:
                self._set_ui_state(BarState.IDLE)
                return
            spoken_hallucinated = looks_hallucinated(spoken.text, speech_ms=spoken.speech_ms)
            if not spoken.confident or spoken_hallucinated:
                # Same hallucination risk as dictation's paste-gate above,
                # for the spoken instruction: a doubtful or implausibly-short
                # transcript here would still spend a full LLM call
                # rewriting the selection from words Whisper likely never
                # actually heard.
                run.annotate(
                    skipped="hallucination_suspected" if spoken_hallucinated else "low_confidence_paste_suppressed"
                )
                self._set_ui_state(
                    BarState.DONE, detail="Not captured — didn't catch that", kind="low_confidence"
                )
                return
            instruction_text = spoken.text

        with run.stage(Stage.COMMAND):
            self._require_groq()
            cfg = self._config.commands
            future = self._network.submit(
                self._groq.cleanup(
                    build_command_user_message(instruction_text, selected_text),
                    system_prompt=COMMAND_SYSTEM_PROMPT,
                    model=cfg.model,
                    temperature=cfg.temperature,
                    reasoning_effort=self._config.cleanup.reasoning_effort or None,
                    max_output_tokens=self._config.cleanup.max_output_tokens or None,
                )
            )
            result = future.result(timeout=self._config.groq.timeout_s + 5.0)
            # normalize_cleanup_output strips the same "Here's the result:"
            # preambles/quoting a model adds around an otherwise-correct
            # transformation; an empty result falls back to leaving the
            # selection untouched rather than pasting nothing over it.
            candidate = normalize_cleanup_output(result.text, selected_text)
            # Every path through this function now ends in the review panel
            # -- a shortcut-fired transform and a spoken voice command alike
            # -- so nothing here pastes without an explicit Apply click.
            # That is what makes ADVISORY the right mode: a tripped
            # guardrail is worth flagging to the human about to decide, but
            # it is never grounds to throw the model's answer away outright
            # the way the dictation path's GATE mode does. Only a genuinely
            # empty response blocks, because there is nothing to show for
            # that. See apply_guardrail's docstring for the full rationale.
            outcome = apply_guardrail(
                selected_text, candidate, mode=GuardrailMode.ADVISORY, expectation=expectation
            )
            if outcome.reason:
                run.annotate(
                    command_guardrail_reason=outcome.reason,
                    command_guardrail_blocked=outcome.blocked,
                )
                if outcome.blocked:
                    logger.warning(
                        "transform output rejected by the %s guardrail; leaving the selection alone",
                        outcome.reason,
                    )
                else:
                    logger.info(
                        "transform output flagged by the %s guardrail; showing it for review",
                        outcome.reason,
                    )

        run.annotate(characters=len(outcome.text))

        if outcome.blocked:
            # Only an empty model response reaches here now -- there is
            # nothing to preview. ERROR (not DONE) so the bar does not draw
            # a success checkmark over a failure, and so it holds for
            # ERROR_HOLD_S instead of the shorter DONE_HOLD_S. No RECOVERY
            # entry: the only honest recovery is "try again", which is what
            # pressing the shortcut/holding the command key already does,
            # and bar.RECOVERY's own docstring is explicit that a button
            # promising to retry and not doing so is worse than no button.
            run.annotate(skipped="command_guardrail")
            self._set_ui_state(
                BarState.ERROR,
                detail="Nothing came back — try again",
                kind="command_guardrail",
            )
            return

        # Preview-first: nothing on this path pastes on its own. The result
        # is handed to the review panel, and only Apply -- a deliberate,
        # separate click -- actually injects it. See
        # _present_transform_result.
        self._present_transform_result(
            transform,
            original=selected_text,
            transformed=outcome.text,
            instruction=instruction_text,
            expectation=expectation,
            guardrail_reason=outcome.reason,
            focused_process=focused_process,
            activation=activation,
            run=run,
        )

    # -- dashboard control (Phase 6) ---------------------------------------

    @property
    def is_listening(self) -> bool:
        with self._listening_lock:
            return self._listening

    def set_listening(self, listening: bool) -> None:
        """Arm or disarm every binding: dictation, command, hands-free, and
        every transform shortcut.

        Idempotent, and safe to call from the dashboard's HTTP thread:
        pynput's listener is started and stopped from arbitrary threads
        already (the tray does the same for the overlay). One shared
        listener now backs all of them, so pausing genuinely stops every
        gesture rather than only the two hold keys -- a transform shortcut
        firing while the app claims to be paused would be a worse surprise
        than a shortcut that briefly stops working until Resume.
        """
        with self._listening_lock:
            if listening == self._listening:
                return
            self._listening = listening
        try:
            if listening:
                self._bindings.start()
                self._bar.set_listening(True)
                self._set_ui_state(BarState.IDLE)
                self._refresh_bar_hint()
            else:
                self._bindings.stop()
                if self._hands_free_active:
                    # The mic must never stay "hot" behind a paused app --
                    # same principle as the dictation/command hold keys,
                    # which cannot be mid-hold when paused since pausing
                    # disarms them; a toggled-on hands-free session has no
                    # such guarantee, so it is force-stopped explicitly.
                    self._stop_hands_free(timed_out=False)
                # PAUSED, not ERROR and not IDLE-with-a-detail. ERROR would
                # auto-expire after a few seconds and leave the bar claiming
                # to be listening while the hotkey was disarmed; IDLE made a
                # paused app and an armed one the same dot on screen. PAUSED
                # renders a hollow dot and offers Resume, so the state is
                # legible at every tier.
                self._bar.set_listening(False)
                self._set_ui_state(BarState.PAUSED)
            logger.info("dictation %s", "resumed" if listening else "paused")
        except Exception:
            logger.exception("could not change the listening state")

    def describe(self) -> dict[str, object]:
        """Live engine facts the databases cannot know, for Settings."""
        hotkey_cfg = self._config.hotkey
        return {
            "hotkey": hotkey_cfg.key,
            "hotkey_chord": hotkey_cfg.key_chord,
            "command_hotkey": hotkey_cfg.command_key,
            "command_hotkey_chord": hotkey_cfg.command_key_chord,
            "hands_free_enabled": self._settings.get().hands_free_enabled,
            "hands_free_key": hotkey_cfg.hands_free_key,
            "hands_free_chord": hotkey_cfg.hands_free_chord,
            "hands_free_double_tap": hotkey_cfg.hands_free_double_tap,
            "hands_free_max_seconds": hotkey_cfg.hands_free_max_seconds,
            "hands_free_active": self._hands_free_active,
            "microphone": self._config.audio.device or "system default",
            "sample_rate": f"{self._config.audio.sample_rate} Hz",
            "transcription_model": WHISPER_MODEL,
            "cleanup_model": self._config.cleanup.model,
            "cleanup_enabled": str(self._config.cleanup.enabled),
            "streaming_enabled": str(self._config.streaming.enabled),
            "typing_wpm_baseline": str(self._config.analytics.typing_wpm),
            "data_directory": str(self._config.data_dir),
            "bindings": [
                {
                    "slot_id": record.slot_id,
                    "owner": record.owner,
                    "label": record.label,
                    "value": record.value,
                    "pretty": record.pretty,
                    "shape": record.shape,
                    "status": record.status,
                    "detail": record.detail,
                    "fire_count": self._binding_fires.get(record.slot_id, (0, 0.0))[0],
                    "last_fired_at": self._binding_fires.get(record.slot_id, (0, 0.0))[1] or None,
                }
                for record in self._binding_ledger
            ],
        }

    def _start_level_pump(self) -> None:
        """Copy the capture thread's level bucket to the pill while recording.

        A small dedicated thread rather than work inside the audio callback:
        the callback must not touch anything but the ring buffer, and the
        overlay only repaints when the bucket actually changes, so polling at
        20 Hz costs a few integer comparisons per second.
        """
        if self._level_pump is not None and self._level_pump.is_alive():
            return

        def pump() -> None:
            while self._hotkey_down or self._hands_free_active:
                level = self._capture.level
                if level != self._input_level:
                    self._input_level = level
                    self._bar.set_level(level)
                time.sleep(0.05)
            self._input_level = 0

        self._level_pump = threading.Thread(target=pump, name="flow-level", daemon=True)
        self._level_pump.start()

    # -- the bar -----------------------------------------------------------

    def _build_bar(self) -> SwayBar:
        """The floating bar, wired to the engine's own actions."""
        return SwayBar(
            position=self._settings.get().overlay_position,
            on_action=self._on_bar_action,
            reduced_motion=self._reduced_motion(),
        )

    def _reduced_motion(self) -> bool:
        """Whether the bar's orb should hold still.

        ``[overlay].reduced_motion`` is the override; ``"auto"`` -- the
        default -- asks the OS, so someone who has turned animation off
        system-wide gets a still orb without having to find a second switch
        in a second place.
        """
        setting = self._config.overlay.reduced_motion
        if setting == "on":
            return True
        if setting == "off":
            return False
        try:
            return not self._platform.animations_enabled()
        except Exception:
            # Fails open, like the adapter itself: a preference we cannot
            # read must not freeze the one part of the bar that says the app
            # is still working.
            logger.debug("could not read the OS animation preference", exc_info=True)
            return False

    def _on_bar_action(self, action: str) -> None:
        """One click on the bar or its menu.

        Runs on the Tk thread, so anything that could block -- a window
        opening, a paste -- is handed to a worker rather than performed here.
        Unknown actions are ignored rather than raising: the bar is allowed
        to offer something this build has not wired up yet.
        """
        handlers: dict[str, Callable[[], None]] = {
            "hide": lambda: self._bar.hide_for(3600),
            "unhide": self._bar.unhide,
            "pause": lambda: self.set_listening(False),
            "resume": lambda: self.set_listening(True),
            "settings": self._open_settings,
            "history": self._open_history,
            "paste": self.paste_last_transcript,
            "copy": self.copy_last_transcript,
            # These two no longer navigate the user away from what they were
            # doing. They answer in place, with the choices themselves.
            ACTION_LANGUAGE: self._open_language_popover,
            self._ACTION_TARGET_MENU: self._open_target_popover,
            ACTION_MIC: self._open_microphone_popover,
        }
        handler = handlers.get(action) or self._popover_choice_handler(action)
        if handler is None:
            logger.debug("bar action %r is not wired up", action)
            return
        threading.Thread(
            target=self._run_bar_action, args=(action, handler), name="sway-bar-action", daemon=True
        ).start()

    # -- the two switchers -------------------------------------------------

    #: Popover rows carry their value in the action string. A prefix keeps
    #: the bar entirely ignorant of what a language or a device is: it
    #: renders rows and reports which one was clicked, exactly as it already
    #: does for the menu.
    _LANGUAGE_PREFIX = "lang:"
    _TARGET_PREFIX = "target:"
    _DEVICE_PREFIX = "dev:"
    #: Not a value, a navigation: opens the output-language list in place.
    _ACTION_TARGET_MENU = "target-menu"

    def _open_language_popover(self) -> None:
        """The dictation language and the output language, in one popover.

        Both live here rather than behind two tray controls because they are
        one question asked twice -- what goes in, what comes out -- and two
        globes at 16px would be indistinguishable. Genuinely live: both are
        re-read on the next dictation (see :meth:`_apply_settings`), so this
        popover needs no caveat and deliberately does not invent one.
        """
        from sway.settings import LANGUAGES, language_label

        settings = self._settings.get()
        current = settings.language
        rows: list[MenuItem] = [MenuItem("", "I'm speaking", enabled=False, footer=True)]
        rows += [
            MenuItem(
                self._LANGUAGE_PREFIX + (code or ""),
                label,
                checked=code == current,
            )
            for code, label in LANGUAGES
        ]
        if current and all(code != current for code, _ in LANGUAGES):
            # A language set outside the shortlist still has to show as the
            # current one, or the popover claims a value that is not in
            # effect.
            rows.insert(
                2,
                MenuItem(
                    self._LANGUAGE_PREFIX + current,
                    language_label(current),
                    checked=True,
                ),
            )

        # The output language is one row that drills down, not a second full
        # list. Both lists inline came to 27 rows -- 1146px, taller than the
        # screen the popover has to open on.
        target = languages.normalize(settings.target_language)
        rows.append(MenuItem(""))
        rows.append(
            MenuItem(
                self._ACTION_TARGET_MENU,
                "Write it in",
                detail=language_label(target) if target else "Same as spoken",
                submenu=True,
            )
        )
        rows.append(MenuItem("settings", "More languages...", "gear"))
        self._bar.open_popover(ACTION_LANGUAGE, tuple(rows), width=232)

    def _open_target_popover(self) -> None:
        """The output language, reached from the language popover.

        "Same as spoken" leads and is the default: it is not a language and
        not a translation, it is what Sway has always done.
        """
        from sway.settings import LANGUAGES, language_label

        target = languages.normalize(self._settings.get().target_language)
        rows: list[MenuItem] = [
            MenuItem(ACTION_LANGUAGE, "Back", "globe"),
            MenuItem("", "Write it in", enabled=False, footer=True),
            MenuItem(self._TARGET_PREFIX, "Same as spoken", checked=target is None),
        ]
        rows += [
            MenuItem(self._TARGET_PREFIX + code, label, checked=code == target)
            for code, label in LANGUAGES
            if code
        ]
        if target and all(code != target for code, _ in LANGUAGES):
            rows.insert(3, MenuItem(self._TARGET_PREFIX + target, language_label(target), checked=True))
        self._bar.open_popover(ACTION_LANGUAGE, tuple(rows), width=232)

    def _open_microphone_popover(self) -> None:
        """The input device, with an honest note about when it applies.

        ``audio_device`` is bound into a stream that CLAUDE.md requires to be
        opened once at startup and held for the process's life, so switching
        it really does need a restart. The footer says so, rather than
        letting the picker look broken.
        """
        try:
            devices = input_devices()
        except Exception:
            logger.exception("could not list input devices for the bar")
            devices = []

        current = self._settings.get().audio_device
        if not devices:
            # The mic-unavailable state the product previously had no answer
            # for: an empty list rendered as nothing at all.
            self._bar.open_popover(
                ACTION_MIC,
                (
                    MenuItem("none", "No microphone found", enabled=False),
                    MenuItem("settings", "Open settings", "gear"),
                ),
                width=248,
            )
            return

        # PortAudio enumerates every device once per host API, so a single
        # microphone arrives three or four times under the same name. The
        # setting stores a *name*, so those rows are not merely redundant --
        # they are indistinguishable, and picking any of them writes the same
        # value. Collapse them.
        seen: set[str] = set()
        unique: list[dict[str, object]] = []
        for device in devices:
            name = str(device.get("name", "")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            unique.append(device)

        # Most-likely first: whatever is selected, then the system default.
        # A picker is for switching, and the row you are switching *from*
        # belongs where you can see it.
        unique.sort(
            key=lambda device: (
                str(device.get("name")) != current,
                not device.get("default"),
            )
        )

        rows = [
            MenuItem(self._DEVICE_PREFIX, "System default", checked=current is None)
        ]
        for device in unique[:_MIC_POPOVER_ROWS]:
            name = str(device.get("name", "")).strip()
            rows.append(
                MenuItem(
                    self._DEVICE_PREFIX + name,
                    name if len(name) <= 26 else name[:25] + "…",
                    checked=name == current,
                    detail="default" if device.get("default") else "",
                )
            )
        if len(unique) > _MIC_POPOVER_ROWS:
            # Truncating silently would hide a device the user owns. Say that
            # the rest exist, and where.
            rows.append(MenuItem("settings", "All microphones…", "gear"))
        rows.append(MenuItem("", label="Applies after a restart", footer=True))
        self._bar.open_popover(ACTION_MIC, tuple(rows), width=272)

    def _popover_choice_handler(self, action: str) -> Callable[[], None] | None:
        """Turn a popover row back into a settings write, or ``None``."""
        if action.startswith(self._TARGET_PREFIX):
            target = action[len(self._TARGET_PREFIX) :] or None
            return lambda: self._choose_target_language(target)
        if action.startswith(self._LANGUAGE_PREFIX):
            language = action[len(self._LANGUAGE_PREFIX) :] or None
            return lambda: self._choose_language(language)
        if action.startswith(self._DEVICE_PREFIX):
            device = action[len(self._DEVICE_PREFIX) :] or None
            return lambda: self._choose_device(device)
        return None

    def _choose_language(self, code: str | None) -> None:
        from sway.settings import language_label

        try:
            self._settings.update(language=code)
        except Exception:
            logger.exception("could not change the dictation language")
            self._set_ui_state(BarState.ERROR, detail="could not save that", kind="settings")
            return
        self._apply_settings()
        # Confirm with the value, not with a tick: the user chose a language,
        # so the confirmation is that language.
        self._set_ui_state(BarState.DONE, detail=language_label(code))

    def _choose_target_language(self, code: str | None) -> None:
        """Set the language the pasted text should come out in.

        ``None`` restores "same as spoken", which is not a translation at all
        and is the default every user starts on.
        """
        from sway.settings import language_label

        normalized = languages.normalize(code)
        try:
            self._settings.update(target_language=normalized)
        except Exception:
            logger.exception("could not change the output language")
            self._set_ui_state(BarState.ERROR, detail="could not save that", kind="settings")
            return
        self._apply_settings()
        self._set_ui_state(
            BarState.DONE,
            detail=language_label(normalized) if normalized else "Same as spoken",
        )

    def _choose_device(self, name: str | None) -> None:
        try:
            self._settings.update(audio_device=name)
        except Exception:
            logger.exception("could not change the input device")
            self._set_ui_state(BarState.ERROR, detail="could not save that", kind="settings")
            return
        self._apply_settings()
        self._set_ui_state(BarState.DONE, detail="Restart to apply")

    def _run_bar_action(self, action: str, handler: Callable[[], None]) -> None:
        try:
            handler()
        except Exception:
            logger.exception("bar action %r failed", action)

    def _open_history(self) -> None:
        self._open_dashboard(fragment="history")

    def _open_settings_at(self, tab: str) -> None:
        # The window reads the fragment on load and again on hashchange, so
        # this focuses an already-open window rather than making a second one.
        # The tab used to be discarded here, which is why every control that
        # claimed to open a particular settings page opened the general one.
        self._open_dashboard(fragment=f"settings/{tab}" if tab else "settings")

    # -- recovering the last transcript ------------------------------------

    def paste_last_transcript(self) -> bool:
        """Put the last dictation back at the cursor.

        The most useful single action in a dictation tool: a paste that the
        target window rejected, a click in the wrong field, an accidental
        undo. Costs nothing -- the text is already in memory.
        """
        text = self._last_transcript
        if not text:
            # Reachable only from the HTTP API now: the bar disables both
            # recovery rows and withholds the inline button when there is
            # nothing stored, so a user cannot ask for this and be refused.
            self._set_ui_state(BarState.ERROR, detail="nothing to paste yet")
            return False
        try:
            self._injector.inject(text)
        except SwayError as exc:
            logger.warning("could not paste the last transcript: %s", exc)
            self._set_ui_state(
                BarState.ERROR,
                detail=_short_failure(exc),
                kind=_describe_failure(exc)["kind"],
            )
            return False
        words = count_words(text)
        self._set_ui_state(BarState.DONE, detail=f"{words} word{'' if words == 1 else 's'}")
        return True

    def copy_last_transcript(self) -> bool:
        """Put the last dictation on the clipboard without pasting it.

        The escape hatch for the window that refuses a synthetic paste --
        an elevated app, a remote desktop -- where the user can still press
        Ctrl+V themselves.
        """
        text = self._last_transcript
        if not text:
            self._set_ui_state(BarState.ERROR, detail="nothing to copy yet")
            return False
        try:
            self._platform.clipboard_set(text)
        except Exception as exc:
            logger.warning("could not copy the last transcript: %s", exc)
            self._set_ui_state(
                BarState.ERROR, detail="clipboard unavailable", kind="clipboard"
            )
            return False
        self._set_ui_state(BarState.DONE, detail="copied")
        return True

    def _remember_transcript(self, text: str) -> None:
        """Hold the last result for the bar's paste/copy actions.

        Memory only, and only the most recent one: history.db already keeps
        the durable record, and this is the "put that back" slot.
        """
        if text:
            self._last_transcript = text
            # Both the menu rows and the capsule's inline recovery button
            # read this, so neither can offer an action with nothing behind
            # it -- which is how the old menu produced its own error message.
            self._bar.set_can_paste(True)

    #: Which successful dictations teach the amend gesture. Two, early, and
    #: then never again: this is a thing to learn once, and a tip that keeps
    #: reappearing is an app talking over its user.
    _AMEND_COACH_AT = (3, 10)

    def _coach_amend_gesture(self) -> None:
        """Teach the one way to change text that has already been pasted.

        Corrections resolve *within* an utterance -- "Monday at 3, no, make it
        Tuesday at 6" reaches the cursor as Tuesday at 6. Once the text has
        landed in another application, a later dictation cannot amend it: the
        user may have typed after it, moved the caret, or switched windows, so
        rewriting it automatically would be a guess that can destroy work.
        Selecting the text and holding the command key turns that guess into a
        statement of intent, which is why this is the gesture and why it is
        worth naming.

        The mechanism already existed and was undiscoverable, which made the
        product look as though it simply could not do this at all.
        """
        try:
            if self._success_count not in self._AMEND_COACH_AT:
                return
            key = self._primary_binding_label(self._effective_command_bindings())
            if not key:
                return
            self._bar.coach(f"Tip → select any text and hold [[{key}]] to change it")
        except Exception:
            logger.debug("could not show the amend-gesture coach", exc_info=True)

    def _refresh_bar_hint(self) -> None:
        """Coach in place until the user has actually dictated something.

        The reference's best idea: guidance appears on the bar, over the real
        desktop, at the moment it is needed -- rather than in a wizard that
        takes the screen and is gone before it is useful. It clears itself
        the moment the user succeeds once.
        """
        try:
            if not self.is_listening:
                self._bar.set_hint("")
                return
            if not self._config.groq_api_key:
                self._bar.set_hint("Add your Groq key in Settings to start")
                return
            if self._last_transcript or not self._should_coach():
                self._bar.set_hint("")
                return
            key = self._primary_binding_label(self._effective_dictation_bindings())
            if not key:
                self._bar.set_hint("Add a dictation shortcut in Settings to start")
                return
            # [[...]] renders as a keycap chip in amber -- the token whose
            # one job is "this is your hotkey" -- rather than as four more
            # words in a sentence the user has to parse.
            self._bar.set_hint(f"Next step → click a textbox, hold [[{key}]]")
        except Exception:
            logger.debug("could not set the bar hint", exc_info=True)

    def _style_for(self, focused_process: str | None) -> CleanupStyle:
        """The cleanup style for the focused app, user rules first.

        A user's explicit rule always beats the built-in table: they can see
        the app in front of them and we are pattern-matching an executable
        name. Falls back to :func:`~flow.profiles.style_for_process` for
        everything they have not spoken about.
        """
        if focused_process:
            target = focused_process.strip().lower()
            for rule in self._settings.get().app_rules:
                if rule.normalised_process() == target:
                    try:
                        return CleanupStyle(rule.style)
                    except ValueError:
                        # A style this build no longer knows. Fall through to
                        # the built-in table rather than failing a dictation.
                        logger.warning("ignoring unknown cleanup style %r", rule.style)
                        break
        return style_for_process(focused_process)

    def _apply_triggers(self, text: str, run: RunRecorder) -> tuple[str, list[Transform]]:
        """Resolve snippets and spoken transforms before anything else.

        Runs before cleanup so a substituted email address or signature is
        never rewritten by the model, and before the network so a purely local
        dictation stays purely local. Never raises: a bug in matching must not
        cost the user their words, so any failure falls back to the untouched
        transcript and no transforms.

        Both kinds go through one matcher (see
        :func:`sway.transforms.as_matchable`). A snippet substitutes its
        payload; a transform substitutes nothing -- its phrase is excised and
        its instruction is returned here, to be folded into the cleanup request
        that was going to happen anyway. That is D8.3's rule: one model call,
        always.
        """
        settings = self._settings.get()
        triggers = settings.triggers
        # Transforms only fire from speech when the model is actually going to
        # run. Excising "make that concise" and then skipping cleanup would
        # delete the words and deliver nothing in exchange -- worse than
        # leaving the phrase in.
        transforms = (
            transforms_module.enabled_with_phrase(settings.transforms)
            if self._config.cleanup.enabled
            else []
        )
        matchable = list(triggers) + transforms_module.as_matchable(transforms)
        if not matchable:
            return text, []
        try:
            result = apply_triggers(text, matchable)
        except Exception:
            logger.exception("trigger matching failed; using the raw transcript")
            return text, []
        if not result.fired:
            return text, []

        by_id = {t.id: t for t in transforms}
        fired = [by_id[hit.trigger_id] for hit in result.hits if hit.trigger_id in by_id]
        snippets = len(result.hits) - len(fired)
        if snippets:
            logger.info("%d snippet(s) fired", snippets)
        if fired:
            logger.info("transform(s) fired: %s", ", ".join(t.name for t in fired))
        run.annotate(
            triggers_fired=snippets,
            transforms_fired=len(fired),
            transform_names=",".join(t.name for t in fired),
        )
        return result.text, fired

    def _record_error(
        self,
        *,
        kind: str,
        headline: str,
        detail: str,
        actionable: bool,
        retry_after_s: float | None = None,
    ) -> None:
        """Remember the last failure so the window can show it.

        Only the most recent is kept: the grouped history already lives in
        latency.db and is rendered on Insights. This is the "what is wrong
        right now" slot, and a list of one is the honest size for it.
        """
        self._last_error = {
            "kind": kind,
            "headline": headline,
            "detail": detail,
            "actionable": actionable,
            "at": time.time(),
            "retry_after_s": retry_after_s,
        }

    def _clear_error(self) -> None:
        """Called after a dictation succeeds, so a stale error never lingers."""
        self._last_error = None

    # -- what the window is allowed to ask of the engine -------------------

    def test_key(self) -> dict[str, Any]:
        """One cheap authenticated call to Groq, reported as a fact.

        Uses the engine's own pre-warmed client rather than building a second
        one, so what this tests is exactly the path a dictation takes. The
        result says what happened and how long it took -- the reference's
        discipline is to report facts, not to show a green tick.
        """
        client = self._groq
        if client is None:
            return {"ok": False, "detail": "Sway's network client is not running."}

        started = time.perf_counter()
        try:
            self._network.submit(client.check_auth()).result(
                timeout=self._config.groq.timeout_s + 5.0
            )
        except GroqRateLimitError as exc:
            # A 429 means the key is *valid* -- it authenticated far enough to
            # be rate limited. Reporting it as a bad key would send the user
            # off to regenerate a key that works.
            return {
                "ok": True,
                "model": WHISPER_MODEL,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "detail": f"Key works, but you are rate limited right now ({exc.limit_type or '429'}).",
            }
        except GroqError as exc:
            return {"ok": False, "detail": str(exc)}
        except TimeoutError:
            return {"ok": False, "detail": "Groq did not respond in time. Check your connection."}
        except Exception as exc:
            logger.exception("key test failed unexpectedly")
            return {"ok": False, "detail": f"Could not reach Groq: {exc}"}

        return {
            "ok": True,
            "model": WHISPER_MODEL,
            "latency_ms": (time.perf_counter() - started) * 1000.0,
        }

    def _apply_settings(self) -> dict[str, Any]:
        """Re-read settings after the UI changed them.

        What can change live and what cannot is stated rather than guessed:
        retention and the overlay's position are read per use, so they take
        effect immediately; the dictation/command/hands-free/transform
        bindings all rebind in place (see BindingListener.rebind); only the
        input device is bound into an open PortAudio stream at startup and
        genuinely needs a restart. The UI says which happened instead of
        silently doing nothing.
        """
        settings = self._settings.load()
        # The key lives in the credential store rather than in settings.json,
        # so it is re-resolved here rather than read off `settings`.
        self._reload_api_key()
        new_config = apply_to_config(self._config, settings)

        try:
            _validate(new_config)
        except ConfigError as exc:
            # Each of these fields is validated individually where it is
            # written (settings.parse, and the dashboard's save actions),
            # but three independently-settable bindings (dictation, command,
            # hands-free) can still combine into something invalid across
            # two separate saves -- this is the door that catches that
            # combination before it reaches a running listener. The setting
            # itself has already been written to disk by the time this runs
            # (see the caller in dashboard.server), so refusing to apply it
            # here must not be mistaken for the save having failed -- it
            # just does not take live effect until the conflict is fixed.
            logger.error("settings produced an invalid configuration; not applying it: %s", exc)
            return {"restart_required": False, "error": str(exc)}

        restart_required = new_config.audio.device != self._config.audio.device

        self._config = new_config

        # Rebuilt fresh only when no session is currently in flight through
        # them -- resetting a tracker mid-session would desync it from
        # self._hotkey_down/_command_key_down and the very next tap would be
        # read as a fresh "start" instead of the stop it should be.
        if not self._hotkey_down:
            self._dictation_chord_tracker = TapModeTracker(
                max_active_s=float(new_config.hotkey.hands_free_max_seconds)
            )
        if not self._command_key_down:
            self._command_chord_tracker = TapModeTracker(
                max_active_s=float(new_config.hotkey.hands_free_max_seconds)
            )

        # Every binding -- dictation, command, hands-free, and every
        # transform -- rebinds in place rather than needing a restart: the
        # OS hooks stay up and only the dispatch tables change, so a binding
        # the user just set works on the very next keystroke.
        self._rebind_all_bindings()

        # Launch-at-login is the one setting that lives outside our own files,
        # so it is pushed to the OS here rather than being read back from it.
        try:
            self._sync_launch_at_login(settings.launch_at_login)
        except Exception:
            logger.warning("could not update the launch-at-login setting", exc_info=True)

        # The bar's coaching depends on the API key and on onboarding, both of
        # which are changed from exactly here. Without this, a user who
        # finishes onboarding keeps being coached until the next restart, and
        # one who adds their key keeps being told to add it.
        self._refresh_bar_hint()

        return {"restart_required": restart_required}

    def _toggle_dictation_via_chord(self) -> None:
        """Dictation's optional alternate binding: a chord fires once per
        tap, so -- unlike the primary hold key -- it starts/stops a session
        rather than being held. Reuses _on_press/_on_release verbatim (the
        same setup and the same _process pipeline a physical hold uses), so
        a chord-started dictation is indistinguishable from a held one once
        it is running -- only how it starts and stops differs.
        """
        outcome = self._dictation_chord_tracker.tap(time.monotonic())
        if outcome is None:
            return
        if outcome == "start":
            self._dictation_started_via_chord = True
            self._on_press()
        else:
            self._on_release()

    def _toggle_command_via_chord(self) -> None:
        """The voice command's optional alternate binding -- same start/stop
        shape as :meth:`_toggle_dictation_via_chord`, but for Command Mode.
        """
        outcome = self._command_chord_tracker.tap(time.monotonic())
        if outcome is None:
            return
        if outcome == "start":
            self._command_started_via_chord = True
            self._on_command_press()
        else:
            self._on_command_release()

    def _transform_handler(self, transform_id: str) -> Callable[[float], None]:
        """A chord handler that runs one transform over the current selection.

        Returns immediately: the handler runs on the listener thread, so
        anything slow here would delay every subsequent keystroke.
        """

        def fire(pressed_at: float) -> None:
            del pressed_at  # transforms don't record audio; nothing to back-date
            self._run_transform(transform_id)

        return fire

    def _run_transform(self, transform_id: str) -> None:
        """Apply a stored transform to whatever is selected in the focused app.

        Reuses the voice-command pipeline wholesale -- same selection capture,
        same system prompt, same single chat completion, same injection -- with
        the instruction coming from storage instead of from speech. That makes
        it strictly cheaper than a spoken command: no recording, no
        transcription request.
        """
        transform = transforms_module.by_id(self._settings.get().transforms, transform_id)
        if transform is None:
            logger.warning("transform %s fired but no longer exists", transform_id)
            return
        if not self._listening:
            logger.info("transform %s ignored: dictation is paused", transform_id)
            return

        # One transform at a time. Belt and braces behind the listener's own
        # per-press debounce (sway.hotkey._TapSlot.tracker): a second run
        # starting while one is in flight would reset self._selected_text
        # out from under the first's still-running capture thread, so the
        # first would find nothing and report "nothing selected" -- and both
        # would spend a Groq call to do it.
        with self._transform_lock:
            if self._transform_running:
                logger.info("transform %s ignored: one is already running", transform.name)
                return
            self._transform_running = True

        self._set_ui_state(BarState.THINKING, detail=f"Reading selection — {transform.name}")
        selection_ready = threading.Event()
        self._selected_text = ""
        focused = self._platform.focused_process_name()

        threading.Thread(
            target=self._capture_selection,
            args=(selection_ready,),
            name="flow-selection",
            daemon=True,
        ).start()
        threading.Thread(
            name="flow-transform",
            target=self._run_transform_worker,
            args=(
                self._ring.mark(),
                focused,
                selection_ready,
                transform.instruction,
                "transform",
                # Its own expectation, not the permissive spoken-command one:
                # a shortcut fires with no spoken confirmation of intent, so
                # only what this transform actually declares is allowed.
                expectation_for([transform]),
                # A shortcut is a tap, not a hold, so there is no release to
                # claim. It still buys the injection guard, so a chord that
                # double-fires transforms the selection once.
                self._activations.once(COMMAND),
            ),
            kwargs={"transform": transform},
            daemon=True,
        ).start()

    def _run_transform_worker(self, *args: Any, **kwargs: Any) -> None:
        """:meth:`_process_command` with the in-flight flag released after it.

        A ``finally`` on the worker thread rather than a callback, so the
        flag is cleared however the run ends -- success, a Groq failure, or
        an unexpected exception -- and a transform can never be wedged off
        by one bad run.
        """
        try:
            self._process_command(*args, **kwargs)
        finally:
            with self._transform_lock:
                self._transform_running = False

    # -- the transform review panel: preview first, apply on demand --------
    #
    # Nothing pastes on its own on this path -- neither a shortcut-run
    # transform nor a spoken voice command over a selection (D8's
    # checkbox-era "opt-in default" became, in the redesign, "preview by
    # default" instead: nothing changes on screen until the user explicitly
    # asks it to). _run_command computes the transformed text and calls
    # _present_transform_result instead of injecting it; everything below is
    # what turns that into a panel and wires Apply/Copy/Retry/Undo back to
    # the actual pipeline.

    def _present_transform_result(
        self,
        transform: Transform | None,
        *,
        original: str,
        transformed: str,
        instruction: str,
        expectation: OutputExpectation,
        guardrail_reason: str,
        focused_process: str | None,
        activation: Activation | None,
        run: RunRecorder,
    ) -> None:
        # A spoken voice command has no stored Transform to point at -- it
        # is reviewed the same way, but transform_id stays "" so Retry
        # (see _transform_retry) knows there is nothing to re-resolve from
        # storage.
        transform_id = transform.id if transform is not None else ""
        transform_name = transform.name if transform is not None else "Voice command"
        self._pending_transform = PendingTransformResult(
            transform_id=transform_id,
            transform_name=transform_name,
            original=original,
            transformed=transformed,
            instruction=instruction,
            expectation=expectation,
            guardrail_reason=guardrail_reason,
            focused_process=focused_process,
            activation=activation,
        )
        if transform is not None:
            self._auto_capture_example(transform, original, transformed)
        parts = word_diff(original, transformed)
        run.annotate(transform_changes=change_count(parts))
        self._set_ui_state(BarState.DONE, detail=f"{transform_name} — see changes")
        self._bar.run_on_ui_thread(lambda root: self._show_transform_panel(root))

    def _auto_capture_example(self, transform: Transform, original: str, transformed: str) -> None:
        """Store this run as the transform's before/after example, if it
        does not already have one.

        Best-effort and silent: a custom transform has no example until it
        has actually been run once, and this is what fills that in without
        asking the user to write one by hand. Never overwrites an existing
        example (including one restored by "reset to defaults"), and never
        touches the shipped defaults' own curated examples.
        """
        if transform.example_input or transform.example_output:
            return
        if not original.strip() or not transformed.strip():
            return
        if original.strip() == transformed.strip():
            # A no-op run (the model legitimately returned the selection
            # unchanged, or a guardrail note left it untouched pending
            # review) makes a useless example -- it would show the same text
            # on both sides of the editor's before/after well forever, since
            # this only ever fires once per transform.
            return
        try:
            current = self._settings.get().transforms
            updated = tuple(
                replace(t, example_input=original, example_output=transformed)
                if t.id == transform.id and not t.example_input
                else t
                for t in current
            )
            self._settings.update(transforms=updated)
        except Exception:
            logger.debug("could not store an example for %r", transform.name, exc_info=True)

    @staticmethod
    def _transform_panel_title(n: int) -> str:
        """"No changes" for a legitimate no-op result (the model returned
        the selection unchanged), never "0 Changes" -- see the blocked-vs-
        no-op distinction in _run_command."""
        if n == 0:
            return "No changes"
        return f"{n} Change" if n == 1 else f"{n} Changes"

    def _show_transform_panel(self, root: tk.Tk) -> None:
        """Runs on the Tk thread only, via SwayBar.run_on_ui_thread."""
        pending = self._pending_transform
        if pending is None:
            return
        if self._transform_panel is None:
            self._transform_panel = TransformReviewPanel(root)
        parts = word_diff(pending.original, pending.transformed)
        self._transform_panel.show(
            title=self._transform_panel_title(change_count(parts)),
            subtitle=pending.transform_name,
            parts=parts,
            applied=pending.applied,
            note=advisory_note(pending.guardrail_reason),
            on_apply=self._transform_apply,
            on_copy=self._transform_copy,
            on_retry=self._transform_retry,
            on_undo=self._transform_undo,
            on_close=self._transform_panel_closed,
        )

    def _refresh_transform_panel(self) -> None:
        pending = self._pending_transform
        panel = self._transform_panel
        if pending is None or panel is None or not panel.is_open:
            return
        parts = word_diff(pending.original, pending.transformed)
        panel.update(
            title=self._transform_panel_title(change_count(parts)),
            parts=parts,
            applied=pending.applied,
            note=advisory_note(pending.guardrail_reason),
        )

    def _transform_apply(self) -> None:
        """Paste the transformed text over the selection. The one action in
        this panel that actually touches the document."""
        pending = self._pending_transform
        if pending is None or pending.applied:
            return
        with self._tracker.run({"source": "transform_apply"}) as run:
            pasted = self._inject_with_fallback(pending.transformed, run, pending.activation)
            if not pasted:
                self._set_ui_state(
                    BarState.ERROR, detail="elevated window — copied instead", kind="paste_blocked"
                )
                return
            pending.applied = True
            # Spent. A later Undo-then-Apply must not replay this token:
            # ActivationLedger.mark_injected only ever honours an activation
            # once, so a second Apply reusing it would be refused and
            # reported as "elevated window -- copied instead" -- wrong on
            # both counts, since that branch returns before copying either.
            # `pending.applied` is what actually guards a repeat Apply; a
            # deliberate second click after Undo doesn't need a fresh
            # single-use token to do the same job again.
            pending.activation = None
            self._record_history(
                kind=KIND_COMMAND,
                text=pending.transformed,
                raw_text=pending.original,
                instruction=pending.instruction,
                focused_process=pending.focused_process,
            )
        self._refresh_transform_panel()

    def _transform_copy(self) -> None:
        """Clipboard only -- no model call, no injection guard needed."""
        pending = self._pending_transform
        if pending is None:
            return
        try:
            self._platform.clipboard_set(pending.transformed)
        except Exception:
            logger.debug("could not copy the transform result", exc_info=True)

    def _transform_retry(self) -> None:
        """Re-run the same instruction over the same original text.

        Costs one more model call, same as any other reviewed rewrite -- the
        panel stays open and its diff refreshes in place once the new
        result is ready, rather than closing and reopening. Works for a
        spoken command too: ``pending.instruction``/``pending.expectation``
        are what get re-sent by default, and a stored transform is only
        consulted (by id) to pick up an instruction edited since this
        result was first shown.
        """
        pending = self._pending_transform
        if pending is None or not self._listening:
            return
        instruction = pending.instruction
        expectation = pending.expectation
        if pending.transform_id:
            transform = transforms_module.by_id(self._settings.get().transforms, pending.transform_id)
            if transform is None:
                return  # the transform was deleted while the panel was open
            instruction = transform.instruction
            expectation = expectation_for([transform])
        original = pending.original
        focused_process = pending.focused_process

        def run_retry() -> None:
            with self._tracker.run({"source": "transform_retry"}) as run:
                self._run_pipeline(
                    run,
                    lambda: self._retry_command(
                        pending, original, instruction, expectation, focused_process, run
                    ),
                    label="command",
                )

        threading.Thread(name="flow-transform-retry", target=run_retry, daemon=True).start()

    def _retry_command(
        self,
        pending: PendingTransformResult,
        original: str,
        instruction: str,
        expectation: OutputExpectation,
        focused_process: str | None,
        run: RunRecorder,
    ) -> None:
        """The retry path: same selection, no fresh capture -- the original
        is already known, so this skips straight to the model call rather
        than re-reading the (possibly now-different) live selection.

        ``pending`` is the exact :class:`PendingTransformResult` this retry
        was launched for, captured by :meth:`_transform_retry` before the
        network round-trip -- identity, not ``transform_id``, is what
        detects whether the panel moved on while this was in flight, which
        also makes this correct for a spoken command's blank
        ``transform_id``.
        """
        with run.stage(Stage.COMMAND):
            self._require_groq()
            cfg = self._config.commands
            future = self._network.submit(
                self._groq.cleanup(
                    build_command_user_message(instruction, original),
                    system_prompt=COMMAND_SYSTEM_PROMPT,
                    model=cfg.model,
                    temperature=cfg.temperature,
                    reasoning_effort=self._config.cleanup.reasoning_effort or None,
                    max_output_tokens=self._config.cleanup.max_output_tokens or None,
                )
            )
            result = future.result(timeout=self._config.groq.timeout_s + 5.0)
            candidate = normalize_cleanup_output(result.text, original)
            # Advisory, same as the first attempt: only an empty response
            # blocks, and it blocks by keeping the *previous* result on
            # screen rather than falling back to the untransformed
            # original -- the thing worth protecting here is what the user
            # was already looking at, not the pre-transform text.
            outcome = apply_guardrail(
                original, candidate, mode=GuardrailMode.ADVISORY, expectation=expectation
            )
            if outcome.blocked:
                run.annotate(
                    command_guardrail_reason=outcome.reason,
                    command_guardrail_blocked=True,
                    skipped="command_guardrail",
                )
                logger.warning("retry produced nothing usable; keeping the previous result")
                if self._pending_transform is pending:
                    self._set_ui_state(
                        BarState.ERROR,
                        detail="Retry came back empty — the previous result is still here",
                        kind="command_guardrail",
                    )
                return
            if outcome.reason:
                run.annotate(command_guardrail_reason=outcome.reason, command_guardrail_blocked=False)

        run.annotate(characters=len(outcome.text))
        if self._pending_transform is not pending:
            return  # the panel was dismissed or moved on while retry was in flight
        pending.transformed = outcome.text
        pending.guardrail_reason = outcome.reason
        pending.applied = False
        self._refresh_transform_panel()

    def _transform_undo(self) -> None:
        """Put the original text back. Only meaningful after Apply."""
        pending = self._pending_transform
        if pending is None or not pending.applied:
            return
        with self._tracker.run({"source": "transform_undo"}) as run:
            pasted = self._inject_with_fallback(pending.original, run, None)
            if not pasted:
                self._set_ui_state(
                    BarState.ERROR, detail="elevated window — copied instead", kind="paste_blocked"
                )
                return
            pending.applied = False
        self._refresh_transform_panel()

    def _transform_panel_closed(self) -> None:
        self._pending_transform = None

    def _sync_launch_at_login(self, enabled: bool) -> None:
        """Add or remove this build from the per-user Run key."""
        if self._platform.launches_at_login(APP_NAME) == enabled:
            return
        command = None
        if enabled:
            executable = Path(sys.executable).resolve()
            # A frozen build launches itself; running from source has to go
            # back through the module so the interpreter is right.
            command = (
                f'"{executable}"'
                if getattr(sys, "frozen", False)
                else f'"{executable}" -m flow run'
            )
        self._platform.set_launch_at_login(APP_NAME, command)

    def _quit_from_ui(self) -> None:
        """Quit from the window's own control, on a thread that can do it.

        Runs on the dashboard's HTTP thread, which must return its response
        before the process goes away -- so the actual shutdown is handed to a
        short-lived thread rather than performed inline.
        """
        logger.info("quit requested from the window")
        threading.Thread(target=self._shutdown_everything, name="flow-quit", daemon=True).start()

    def _shutdown_everything(self) -> None:
        try:
            self.stop()
        except Exception:
            logger.exception("shutdown from the window failed")
        finally:
            self._bar.stop()

    def _ensure_dashboard(self) -> DashboardServer | None:
        """Start the loopback server if it is not already up. Never raises.

        Started eagerly at :meth:`start` rather than on the first tray click,
        because D1's second-launch focus needs a port to advertise from the
        moment the app is running -- a user who double-clicks the icon again
        has not necessarily opened the dashboard first.
        """
        if self._dashboard is not None:
            return self._dashboard
        try:
            server = DashboardServer(
                history_db=self._config.history_db_path,
                latency_db=self._config.latency_db_path,
                dictionary_db=self._config.dictionary_db_path,
                typing_wpm=self._config.analytics.typing_wpm,
                settings=self._settings,
                log_file=self._config.log_file_path,
                control=EngineControl(
                    is_listening=lambda: self.is_listening,
                    set_listening=self.set_listening,
                    describe=self.describe,
                    focus=self._focus_from_second_launch,
                    test_key=self.test_key,
                    on_settings_changed=self._apply_settings,
                    quit=self._quit_from_ui,
                    last_error=lambda: self._last_error,
                    input_level=lambda: self._input_level,
                    hotkey_down=lambda: self._hotkey_down,
                    bindings_down=lambda: {
                        "command": self._command_key_down,
                        "dictation_chord": self._dictation_chord_tracker.active,
                        "command_chord": self._command_chord_tracker.active,
                    },
                    hands_free_active=lambda: self._hands_free_active,
                ),
            )
            server.start()
        except Exception:
            logger.exception("could not start the dashboard server")
            return None
        self._dashboard = server
        return server

    def _publish_instance(self) -> None:
        """Advertise this instance so a second launch can focus it."""
        server = self._ensure_dashboard()
        if server is None:
            logger.info("no dashboard server; a second launch will show a dialog instead")
            return
        instance.publish(
            self._config.data_dir,
            port=server.port,
            token=server.token,
            url=server.url,
        )

    def _focus_from_second_launch(self) -> None:
        """Someone double-clicked Sway while it was already running.

        Runs on the dashboard's HTTP thread. Opening the dashboard is the
        most useful interpretation of "show me the app": the tray icon is
        already visible, so simply raising that would look like nothing
        happened.
        """
        logger.info("second launch asked us to come forward")
        try:
            self._open_dashboard()
        except Exception:
            logger.exception("could not come forward for a second launch")

    def _open_settings(self) -> None:
        """Open the window on Settings.

        The window is a single-page app, so this is the same window with a
        hash the front end reads on load -- not a second surface. Settings
        living in exactly one place is the point.
        """
        self._open_dashboard(fragment="settings")

    def _open_dashboard(self, *, fragment: str = "") -> None:
        """Show the application window, starting the server behind it if needed.

        Three tiers, each a real fallback rather than an error:

        1. A native window (WebView2). What a user should always get.
        2. The default browser, if that runtime is missing.
        3. A dialog with the address, if even that fails.

        Best-effort throughout: a window that will not open logs and leaves
        dictation completely unaffected.
        """
        server = self._ensure_dashboard()
        if server is None:
            self._platform.show_message(
                APP_NAME,
                "Sway's window could not start.\n\n"
                f"Details are in:\n{self._config.log_file_path}",
                error=True,
            )
            return

        url = f"{server.url}#{fragment}" if fragment else server.url
        if self._window is not None and fragment:
            # Navigate the existing window rather than making a second one,
            # so "Settings" from the tray focuses the app the user already
            # has instead of surprising them with a duplicate.
            self._window.navigate(url)

        # Logged before the attempt, so "the menu item did nothing" can be
        # told apart from "the window refused to open" in the log alone.
        logger.info("showing the application window")
        if self._window is not None and self._window.show():
            return

        logger.info("no native window available; falling back to a browser")
        if self._platform.open_url(url):
            return

        # Every mechanism failed. The server is running and reachable, so the
        # address is still useful -- show it rather than leaving the click
        # looking like it did nothing.
        self._platform.show_message(
            APP_NAME,
            "Sway could not open its window.\n\n"
            "Sway is still running and will keep dictating. "
            "You can reach its window at:\n\n"
            f"{server.url}",
            error=True,
        )

    # -- history (Phase 5) -------------------------------------------------

    def _record_history(
        self,
        *,
        kind: str,
        text: str,
        raw_text: str,
        instruction: str | None = None,
        focused_process: str | None = None,
        run_id: str | None = None,
        metrics: SessionMetrics | None = None,
        language: str | None = None,
        status: str | None = None,
    ) -> None:
        if self._history is None:
            return
        try:
            self._history.add(
                kind=kind,
                text=text,
                raw_text=raw_text,
                instruction=instruction,
                focused_process=focused_process,
                run_id=run_id,
                metrics=metrics,
                language=language,
                status=status,
            )
        except HistoryStoreError as exc:
            logger.warning("could not record history entry: %s", exc)


__all__ = ["Engine", "should_process"]
