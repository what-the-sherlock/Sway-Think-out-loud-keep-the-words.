"""Wires capture, VAD, FLAC encoding, Groq transcription, LLM cleanup, and
paste injection into the dictation pipeline, with every stage boundary
measured by the Phase 0 :class:`~flow.latency.tracker.LatencyTracker`.

Threading model (see CLAUDE.md's architecture rules for why):

* :class:`~flow.audio.capture.AudioCapture` opens the mic once at
  :meth:`Engine.start` and its callback only writes into the ring buffer.
* :class:`~flow.hotkey.HotkeyListener` runs pynput's own listener thread, one
  for the dictation key and one for the command key (Phase 5). Each
  ``on_release`` hands its recorded span off to a fresh worker thread rather
  than processing inline, so the listener is free to see the next key event
  immediately.
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
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sway import APP_NAME, instance
from sway.audio.capture import AudioCapture
from sway.analytics import SessionMetrics, count_words, pcm_duration_ms, session_metrics

from sway.audio.flac import encode_flac
from sway.audio.ring_buffer import RingBuffer
from sway.audio.vad import VadTrimmer
from sway.cleanup import (
    CleanupStats,
    is_cleanup_output_safe,
    normalize_cleanup_output,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from sway.commands import COMMAND_SYSTEM_PROMPT, build_command_user_message
from sway.config import Config
from sway.corrections import CorrectionScan
from sway.corrections import scan as scan_corrections
from sway.dashboard.server import DashboardServer, EngineControl
from sway.dictionary import DictionaryStore, build_prompt_bias
from sway.errors import (
    AudioError,
    ClipboardError,
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
from sway.hotkey import KNOWN_KEYS, HotkeyListener, prettify_hotkey
from sway.inject import TextInjector
from sway.latency.models import Stage
from sway.latency.tracker import LatencyTracker, RunRecorder
from sway.net_loop import NetworkLoop
from sway.bar import BarState, NullBar, SwayBar
from sway.platform import PlatformAdapter, get_platform
from sway.profiles import CleanupStyle, style_for_process
from sway.selection import SelectionReader
from sway.settings import SettingsStore, apply_to_config
from sway.triggers import apply_triggers
from sway.streaming import ChunkSessionResult, ChunkStreamer, max_overlap_words, stitch_transcripts
from sway.tray import TrayIcon, TrayState
from sway.window import AppWindow

logger = logging.getLogger(__name__)


def should_process(held_ms: float, min_hold_ms: int) -> bool:
    """Pure guard: was the hold long enough to be a deliberate dictation?"""
    return held_ms >= min_hold_ms


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
        self._injector = TextInjector(self._platform, restore_delay_ms=config.paste.restore_delay_ms)
        self._selection_reader = SelectionReader(
            self._platform, timeout_s=config.commands.selection_timeout_s
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

        self._hotkey = HotkeyListener(
            key=KNOWN_KEYS[config.hotkey.key],
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._command_hotkey: HotkeyListener | None = None
        if config.hotkey.command_key is not None:
            self._command_hotkey = HotkeyListener(
                key=KNOWN_KEYS[config.hotkey.command_key],
                on_press=self._on_command_press,
                on_release=self._on_command_release,
            )

        self._preroll_frames = round(config.audio.preroll_ms / 1000.0 * config.audio.sample_rate)
        self._start_frame = 0
        self._press_time = 0.0
        self._release_time = 0.0
        self._focused_process: str | None = None
        self._stream_session: ChunkStreamer | None = None

        self._command_press_time = 0.0
        self._command_start_frame = 0
        self._command_focused_process: str | None = None
        self._selection_ready = threading.Event()
        self._selected_text = ""

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Opens the mic, starts the network loop and Groq client, arms the hotkey(s)."""
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
        self._hotkey.start()
        if self._command_hotkey is not None:
            self._command_hotkey.start()
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
        logger.info("Sway ready. Hold %s to dictate.", prettify_hotkey(self._config.hotkey.key))
        if self._command_hotkey is not None:
            logger.info(
                "Hold %s with text selected to run a voice command.",
                prettify_hotkey(self._config.hotkey.command_key),
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
        for label, action in (
            ("hotkey", self._hotkey.stop),
            ("command hotkey", self._stop_command_hotkey),
            ("tray", self._stop_tray),
            ("instance file", self._withdraw_instance),
            ("window", self._stop_window),
            ("dashboard", self._stop_dashboard),
            ("capture", self._capture.stop),
            ("network", self._stop_network),
            ("dictionary", self._stop_dictionary),
            ("history", self._stop_history),
            ("overlay", self._bar.stop),
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

    def _stop_command_hotkey(self) -> None:
        if self._command_hotkey is not None:
            self._command_hotkey.stop()

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
        BarState.ERROR: TrayState.ERROR,
    }

    def _set_ui_state(self, state: BarState, *, detail: str = "") -> None:
        """Drive the overlay and the tray icon together.

        A paused engine always shows DISABLED in the tray regardless of the
        overlay state it was given. set_listening deliberately puts the
        overlay into IDLE-with-detail rather than ERROR when pausing (see
        there for why), and without this override the tray would inherit that
        IDLE and claim to be armed while the hotkey was disarmed.
        """
        self._bar.set_state(state, detail=detail) if detail else self._bar.set_state(state)
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

    # -- dictation hotkey callbacks (called from the pynput listener thread) --

    def _on_press(self) -> None:
        self._press_time = time.monotonic()
        # Read by /api/status so onboarding can show the key lighting up as
        # it is physically held. A plain bool assignment, so the listener
        # thread is not slowed by it.
        self._hotkey_down = True
        self._start_frame = preroll_start_frame(self._ring.mark(), self._preroll_frames)
        # Captured at press time, when the target app is unambiguously the one
        # the user is dictating into; PlatformAdapter guarantees this never
        # raises, so no try/except is needed on this hot path.
        self._focused_process = self._platform.focused_process_name()
        # Metering runs only while the key is held, so the steady-state audio
        # callback stays a bounds-checked memcpy and nothing more.
        self._capture.set_level_metering(True)
        self._start_level_pump()
        self._set_ui_state(BarState.RECORDING)
        self._stream_session = self._start_streaming() if self._config.streaming.enabled else None

    def _on_release(self) -> None:
        release_time = time.monotonic()
        self._hotkey_down = False
        self._capture.set_level_metering(False)
        self._input_level = 0
        self._bar.set_level(0)
        held_ms = (release_time - self._press_time) * 1000.0
        session, self._stream_session = self._stream_session, None
        if not should_process(held_ms, self._config.hotkey.min_hold_ms):
            if session is not None:
                session.stop(timeout=0.5)
            self._set_ui_state(BarState.IDLE)
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
            ),
            daemon=True,
        ).start()

    # -- streaming (Phase 4): background chunks while the key is held ----

    def _start_streaming(self) -> ChunkStreamer:
        streaming = self._config.streaming
        session = ChunkStreamer(
            ring=self._ring,
            vad=self._vad,
            encode=lambda pcm: encode_flac(
                pcm, self._config.audio.sample_rate, self._config.audio.channels
            ),
            submit_transcribe=self._submit_chunk_transcribe,
            start_frame=self._start_frame,
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
                flac_bytes, language=self._config.groq.language, prompt=prompt_bias or None
            )
        )

    # -- command hotkey callbacks (Phase 5; called from the listener thread) --

    def _on_command_press(self) -> None:
        self._command_press_time = time.monotonic()
        self._command_start_frame = preroll_start_frame(self._ring.mark(), self._preroll_frames)
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
        held_ms = (time.monotonic() - self._command_press_time) * 1000.0
        if not should_process(held_ms, self._config.hotkey.min_hold_ms):
            self._set_ui_state(BarState.IDLE)
            return
        self._set_ui_state(BarState.THINKING)
        threading.Thread(
            name="flow-command",
            target=self._process_command,
            args=(self._command_start_frame, self._command_focused_process, self._selection_ready),
            daemon=True,
        ).start()

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
            self._set_ui_state(BarState.ERROR, detail=detail)
        except TimeoutError as exc:
            run.mark_failed(exc)
            logger.error("%s timed out: %s", label, exc)
            self._record_error(
                kind="timeout",
                headline="Groq did not respond in time",
                detail="Retrying next time. Check your connection if it keeps happening.",
                actionable=False,
            )
            self._set_ui_state(BarState.ERROR, detail="timed out")
        except (GroqError, SwayError) as exc:
            run.mark_failed(exc)
            logger.error("%s failed: %s", label, exc)
            self._record_error(**_describe_failure(exc))
            self._set_ui_state(BarState.ERROR, detail=_short_failure(exc))
        except Exception as exc:  # never let a bug hang the app with the mic hot
            run.mark_failed(exc)
            logger.exception("unexpected %s failure", label)
            self._record_error(
                kind="unexpected",
                headline="Something went wrong",
                detail="The details are in Sway's log file.",
                actionable=False,
            )
            self._set_ui_state(BarState.ERROR, detail="failed")

    def _process(
        self,
        start_frame: int,
        focused_process: str | None,
        session: ChunkStreamer | None,
        release_time: float,
        held_ms: float,
        press_time: float,
    ) -> None:
        with self._tracker.run({"source": "hotkey"}) as run:
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
                run, lambda: self._run_dictation(start_frame, focused_process, session, run)
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
    ) -> None:
        transcript = self._transcribe(start_frame, run, session)
        if not transcript:
            self._set_ui_state(BarState.IDLE)
            return

        # Local, zero-cost substitutions happen before the model sees the
        # text, so a pasted signature or address is never "tidied up".
        triggered = self._apply_triggers(transcript.text, run)
        if triggered != transcript.text:
            transcript = replace(transcript, text=triggered)

        # Local, pure, sub-millisecond. Runs after triggers so a substituted
        # macro cannot be mistaken for a retraction, and before cleanup
        # because its result decides whether cleanup runs at all.
        correction = self._scan_corrections(transcript.text)

        cleaned_text = self._clean_up(transcript, run, focused_process, correction)

        self._set_ui_state(BarState.INJECTING)
        with run.stage(Stage.INJECT):
            pasted = self._inject_with_fallback(cleaned_text, run)

        if pasted:
            # Confirm with a fact, not a tick: the word count also silently
            # answers "did anything actually land?", which is the failure the
            # user is most likely to be watching for.
            words = count_words(cleaned_text)
            self._remember_transcript(cleaned_text)
            self._set_ui_state(
                BarState.DONE, detail=f"{words} word{'' if words == 1 else 's'}"
            )
            self._refresh_bar_hint()
            # A success clears the "what is wrong right now" slot, so Home
            # never shows a stale failure the user has already worked past.
            self._clear_error()
        else:
            self._set_ui_state(
                BarState.ERROR, detail="elevated window — copied instead"
            )

        # Everything below runs *after* the text is on screen, so none of it
        # counts against the sub-second budget CLAUDE.md sets.
        metrics = self._session_metrics(cleaned_text, transcript, start_frame)
        run.annotate(
            words=metrics.word_count,
            wpm=round(metrics.wpm, 1) if metrics.wpm is not None else "",
            time_saved_s=round(metrics.time_saved_s, 1),
        )
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

    def _inject_with_fallback(self, text: str, run: RunRecorder) -> bool:
        """Paste ``text``, or -- when the focused window is confidently
        elevated (:meth:`~flow.platform.PlatformAdapter.paste_likely_blocked`)
        and a synthetic paste would silently do nothing -- leave it on the
        clipboard instead so the user can paste it themselves with a real
        keystroke, which UIPI does not block. Returns whether the paste
        itself succeeded (``False`` on the clipboard-only fallback).
        """
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
                    on_first_byte=lambda: run.end(first_byte),
                )
            )
            result = future.result(timeout=self._config.groq.timeout_s + 5.0)

        run.annotate(
            characters=len(result.text),
            language=result.language or "",
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
        return _Transcript(text=merged, language=tail_language, confident=tail_confident)

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
            terms = self._dictionary.list_terms()
        except DictionaryStoreError as exc:
            logger.warning("could not read the personal dictionary: %s", exc)
            return ""
        return build_prompt_bias(terms, max_chars=self._config.dictionary.max_prompt_chars)

    def _clean_up(
        self,
        transcript: _Transcript,
        run: RunRecorder,
        focused_process: str | None,
        correction: CorrectionScan | None = None,
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
        skip_reason = self._cleanup_skip_reason(transcript, style, correction)
        if skip_reason:
            cleaned_text = raw_text
        else:
            try:
                with run.stage(Stage.CLEANUP):
                    cleaned_text, guardrail = self._request_cleanup(
                        raw_text, style, transcript, run, correction
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
            transcript_confident=transcript.confident,
            correction_detected=correction.detected,
            correction_markers=correction.describe(),
            correction_kinds=",".join(correction.kinds),
        )
        return cleaned_text

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
        self, transcript: _Transcript, style: CleanupStyle, correction: CorrectionScan
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
        if correction.detected:
            return ""
        if cfg.skip_when_unconfident and not transcript.confident:
            return "low_confidence"
        if should_skip_cleanup(
            transcript.text, language=transcript.language, min_words=cfg.skip_min_words
        ):
            return "short_and_clean"
        return ""

    def _request_cleanup(
        self,
        raw_text: str,
        style: CleanupStyle,
        transcript: _Transcript,
        run: RunRecorder,
        correction: CorrectionScan,
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
        future = self._network.submit(
            self._groq.cleanup(
                raw_text,
                system_prompt=system_prompt_for_style(
                    style,
                    language=transcript.language,
                    correction=correction.detected,
                    remove_fillers=self._settings.get().remove_fillers,
                ),
                model=cfg.model,
                temperature=cfg.temperature,
                reasoning_effort=cfg.reasoning_effort or None,
                max_output_tokens=cfg.max_output_tokens or None,
                on_first_token=lambda: run.end(first_token),
            )
        )
        result = future.result(timeout=self._config.groq.timeout_s + 5.0)
        candidate = normalize_cleanup_output(result.text, raw_text)
        if not candidate:
            return raw_text, "empty"
        # The entity guard treats every number as protected, so a *correct*
        # rewrite of "at 7am, no no, at 8am" looks exactly like the model
        # having lost the 7. Handing it the retracted span is what tells the
        # two apart -- and only the retracted span, so every other figure in
        # the utterance stays protected.
        retracted = correction.retracted_text()
        if not is_cleanup_output_safe(
            raw_text,
            candidate,
            retracted=retracted,
            correction_detected=correction.detected,
        ):
            reason = unsafe_reason(
                raw_text,
                candidate,
                retracted=retracted,
                correction_detected=correction.detected,
            )
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
        self, start_frame: int, focused_process: str | None, selection_ready: threading.Event
    ) -> None:
        with self._tracker.run({"source": "command"}) as run:
            self._run_pipeline(
                run,
                lambda: self._run_command(start_frame, focused_process, selection_ready, run),
                label="command",
            )

    def _run_command(
        self,
        start_frame: int,
        focused_process: str | None,
        selection_ready: threading.Event,
        run: RunRecorder,
    ) -> None:
        with run.stage(Stage.SELECTION):
            selection_ready.wait(timeout=self._config.commands.selection_timeout_s + 1.0)
            selected_text = self._selected_text
        if not selected_text:
            run.annotate(skipped="empty_selection")
            self._set_ui_state(BarState.ERROR, detail="nothing selected")
            return

        instruction = self._transcribe_single_shot(start_frame, run)
        if not instruction:
            self._set_ui_state(BarState.IDLE)
            return

        with run.stage(Stage.COMMAND):
            self._require_groq()
            cfg = self._config.commands
            future = self._network.submit(
                self._groq.cleanup(
                    build_command_user_message(instruction.text, selected_text),
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
            transformed = normalize_cleanup_output(result.text, selected_text) or selected_text

        with run.stage(Stage.INJECT):
            pasted = self._inject_with_fallback(transformed, run)
        self._set_ui_state(
            BarState.IDLE if pasted else BarState.ERROR,
            detail="" if pasted else "elevated window — copied instead",
        )

        run.annotate(characters=len(transformed))
        self._record_history(
            kind=KIND_COMMAND,
            text=transformed,
            raw_text=selected_text,
            instruction=instruction.text,
            focused_process=focused_process,
        )

    # -- dashboard control (Phase 6) ---------------------------------------

    @property
    def is_listening(self) -> bool:
        with self._listening_lock:
            return self._listening

    def set_listening(self, listening: bool) -> None:
        """Arm or disarm the dictation hotkey.

        Idempotent, and safe to call from the dashboard's HTTP thread:
        pynput's listener is started and stopped from arbitrary threads
        already (the tray does the same for the overlay).
        """
        with self._listening_lock:
            if listening == self._listening:
                return
            self._listening = listening
        try:
            if listening:
                self._hotkey.start()
                if self._command_hotkey is not None:
                    self._command_hotkey.start()
                self._set_ui_state(BarState.IDLE)
            else:
                self._hotkey.stop()
                if self._command_hotkey is not None:
                    self._command_hotkey.stop()
                # IDLE, not ERROR: a pause is a deliberate state, not a
                # failure, and the overlay auto-clears ERROR back to IDLE
                # after a few seconds -- which would leave the pill claiming
                # to be listening while the hotkey was still disarmed. IDLE
                # renders its detail text as the label, so this reads
                # "Paused" and stays there until resumed.
                self._set_ui_state(BarState.IDLE, detail="Paused")
            logger.info("dictation %s", "resumed" if listening else "paused")
        except Exception:
            logger.exception("could not change the listening state")

    def describe(self) -> dict[str, object]:
        """Live engine facts the databases cannot know, for Settings."""
        return {
            "hotkey": self._config.hotkey.key,
            "command_hotkey": self._config.hotkey.command_key,
            "microphone": self._config.audio.device or "system default",
            "sample_rate": f"{self._config.audio.sample_rate} Hz",
            "transcription_model": WHISPER_MODEL,
            "cleanup_model": self._config.cleanup.model,
            "cleanup_enabled": str(self._config.cleanup.enabled),
            "streaming_enabled": str(self._config.streaming.enabled),
            "typing_wpm_baseline": str(self._config.analytics.typing_wpm),
            "data_directory": str(self._config.data_dir),
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
            while self._hotkey_down:
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
        )

    def _on_bar_action(self, action: str) -> None:
        """One click on the bar or its menu.

        Runs on the Tk thread, so anything that could block -- a window
        opening, a paste -- is handed to a worker rather than performed here.
        Unknown actions are ignored rather than raising: the bar is allowed
        to offer something this build has not wired up yet.
        """
        handlers: dict[str, Callable[[], None]] = {
            "hide": lambda: self._bar.hide_for(3600),
            "settings": self._open_settings,
            "history": self._open_history,
            "paste": self.paste_last_transcript,
            "copy": self.copy_last_transcript,
            "language": lambda: self._open_settings_at("voice"),
            "microphone": lambda: self._open_settings_at("voice"),
        }
        handler = handlers.get(action)
        if handler is None:
            logger.debug("bar action %r is not wired up", action)
            return
        threading.Thread(
            target=self._run_bar_action, args=(action, handler), name="sway-bar-action", daemon=True
        ).start()

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
        self._open_dashboard(fragment="settings")
        del tab

    # -- recovering the last transcript ------------------------------------

    def paste_last_transcript(self) -> bool:
        """Put the last dictation back at the cursor.

        The most useful single action in a dictation tool: a paste that the
        target window rejected, a click in the wrong field, an accidental
        undo. Costs nothing -- the text is already in memory.
        """
        text = self._last_transcript
        if not text:
            self._set_ui_state(BarState.ERROR, detail="nothing to paste yet")
            return False
        try:
            self._injector.inject(text)
        except SwayError as exc:
            logger.warning("could not paste the last transcript: %s", exc)
            self._set_ui_state(BarState.ERROR, detail=_short_failure(exc))
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
            self._set_ui_state(BarState.ERROR, detail="clipboard unavailable")
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

    def _refresh_bar_hint(self) -> None:
        """Coach in place until the user has actually dictated something.

        The reference's best idea: guidance appears on the bar, over the real
        desktop, at the moment it is needed -- rather than in a wizard that
        takes the screen and is gone before it is useful. It clears itself
        the moment the user succeeds once.
        """
        try:
            if self._last_transcript:
                self._bar.set_hint("")
                return
            if not self._config.groq_api_key:
                self._bar.set_hint("Add your Groq key in Settings to start")
                return
            key = prettify_hotkey(self._config.hotkey.key)
            self._bar.set_hint(f"Next step: click a textbox, then hold {key}")
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

    def _apply_triggers(self, text: str, run: RunRecorder) -> str:
        """Substitute the user's zero-cost triggers before anything else.

        Runs before cleanup so a substituted email address or signature is
        never rewritten by the model, and before the network so a purely
        local dictation stays purely local. Never raises: a bug in trigger
        matching must not cost the user their words, so any failure falls
        back to the untouched transcript.
        """
        triggers = self._settings.get().triggers
        if not triggers:
            return text
        try:
            result = apply_triggers(text, triggers)
        except Exception:
            logger.exception("trigger matching failed; using the raw transcript")
            return text
        if not result.fired:
            return text
        logger.info("%d trigger(s) fired", len(result.hits))
        run.annotate(triggers_fired=len(result.hits))
        return result.text

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
            self._network.run(client.check_auth())
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
        effect immediately; the hotkey and the input device are bound into a
        listener and an open stream at startup, so they need a restart. The
        UI says which happened instead of silently doing nothing.
        """
        settings = self._settings.load()
        # The key lives in the credential store rather than in settings.json,
        # so it is re-resolved here rather than read off `settings`.
        self._reload_api_key()
        new_config = apply_to_config(self._config, settings)

        restart_required = (
            new_config.hotkey.key != self._config.hotkey.key
            or new_config.audio.device != self._config.audio.device
        )

        self._config = new_config

        # Launch-at-login is the one setting that lives outside our own files,
        # so it is pushed to the OS here rather than being read back from it.
        try:
            self._sync_launch_at_login(settings.launch_at_login)
        except Exception:
            logger.warning("could not update the launch-at-login setting", exc_info=True)

        return {"restart_required": restart_required}

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
