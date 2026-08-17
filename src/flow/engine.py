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
import threading
import time
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

from flow.audio.capture import AudioCapture
from flow.audio.flac import encode_flac
from flow.audio.ring_buffer import RingBuffer
from flow.audio.vad import VadTrimmer
from flow.cleanup import (
    CleanupStats,
    is_cleanup_output_safe,
    normalize_cleanup_output,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from flow.commands import COMMAND_SYSTEM_PROMPT, build_command_user_message
from flow.config import Config
from flow.dictionary import DictionaryStore, build_prompt_bias
from flow.errors import DictionaryStoreError, FlowError, HistoryStoreError, PasteBlockedError
from flow.groq_client import GroqClient, GroqError, GroqRateLimitError, TranscriptionResult
from flow.history import KIND_COMMAND, KIND_DICTATION, HistoryStore
from flow.history_window import HistoryWindow
from flow.hotkey import KNOWN_KEYS, HotkeyListener
from flow.inject import TextInjector
from flow.latency.models import Stage
from flow.latency.tracker import LatencyTracker, RunRecorder
from flow.net_loop import NetworkLoop
from flow.overlay import NullOverlay, Overlay, OverlayState
from flow.platform import PlatformAdapter, get_platform
from flow.profiles import CleanupStyle, style_for_process
from flow.selection import SelectionReader
from flow.streaming import ChunkSessionResult, ChunkStreamer, max_overlap_words, stitch_transcripts
from flow.tray import TrayIcon

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


class Engine:
    """Owns every long-lived resource for one run of the app."""

    def __init__(self, config: Config, tracker: LatencyTracker) -> None:
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
        self._overlay = Overlay() if config.overlay.enabled else NullOverlay()
        self._network = NetworkLoop()
        self._groq: GroqClient | None = None
        self._cleanup_stats = CleanupStats()
        self._dictionary: DictionaryStore | None = None
        self._history: HistoryStore | None = None
        self._history_window: HistoryWindow | None = None
        self._tray: TrayIcon | None = None

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
        self._groq = self._network.submit(self._build_groq_client()).result(timeout=10.0)
        self._network.submit(self._groq.warm_up())
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
            self._tray = TrayIcon(on_show_history=self._open_history_window, on_quit=self._overlay.stop)
            if not self._tray.start():
                self._tray = None
        logger.info("Flow ready. Hold %s to dictate.", self._config.hotkey.key)
        if self._command_hotkey is not None:
            logger.info("Hold %s with text selected to run a voice command.", self._config.hotkey.command_key)

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

    async def _build_groq_client(self) -> GroqClient:
        return GroqClient(
            self._config.groq_api_key,
            base_url=self._config.groq.base_url,
            timeout_s=self._config.groq.timeout_s,
            connect_timeout_s=self._config.groq.connect_timeout_s,
            max_retries=self._config.groq.max_retries,
        )

    def run_overlay(self) -> None:
        """Blocks the calling thread (the overlay's UI thread) until shutdown."""
        self._overlay.run()

    def stop(self) -> None:
        """Idempotent; safe to call even if :meth:`start` only partly succeeded."""
        for label, action in (
            ("hotkey", self._hotkey.stop),
            ("command hotkey", self._stop_command_hotkey),
            ("tray", self._stop_tray),
            ("capture", self._capture.stop),
            ("network", self._stop_network),
            ("dictionary", self._stop_dictionary),
            ("history", self._stop_history),
            ("overlay", self._overlay.stop),
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

    def _stop_tray(self) -> None:
        if self._tray is not None:
            self._tray.stop()

    # -- history window (Phase 5): opened from the tray, shown on the Tk thread

    def _open_history_window(self) -> None:
        """Called from the tray icon's own thread; never touches Tk directly."""
        if self._history is None:
            logger.info("history window requested, but [history].enabled = false")
            return
        self._overlay.run_on_ui_thread(self._show_history_window)

    def _show_history_window(self, root: tk.Tk) -> None:
        """Runs on the Tk thread only, via Overlay.run_on_ui_thread."""
        assert self._history is not None
        if self._history_window is None:
            self._history_window = HistoryWindow(root, self._history)
        self._history_window.show()

    # -- dictation hotkey callbacks (called from the pynput listener thread) --

    def _on_press(self) -> None:
        self._press_time = time.monotonic()
        self._start_frame = preroll_start_frame(self._ring.mark(), self._preroll_frames)
        # Captured at press time, when the target app is unambiguously the one
        # the user is dictating into; PlatformAdapter guarantees this never
        # raises, so no try/except is needed on this hot path.
        self._focused_process = self._platform.focused_process_name()
        self._overlay.set_state(OverlayState.RECORDING)
        self._stream_session = self._start_streaming() if self._config.streaming.enabled else None

    def _on_release(self) -> None:
        release_time = time.monotonic()
        held_ms = (release_time - self._press_time) * 1000.0
        session, self._stream_session = self._stream_session, None
        if not should_process(held_ms, self._config.hotkey.min_hold_ms):
            if session is not None:
                session.stop(timeout=0.5)
            self._overlay.set_state(OverlayState.IDLE)
            return
        self._overlay.set_state(OverlayState.THINKING)
        # Hand off immediately: the listener thread must stay free for the
        # next key event, and this thread may block on the network.
        threading.Thread(
            target=self._process,
            args=(self._start_frame, self._focused_process, session, release_time, held_ms),
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
        assert self._groq is not None
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
        self._overlay.set_state(OverlayState.RECORDING)
        selection_ready = threading.Event()
        self._selection_ready = selection_ready
        self._selected_text = ""
        # The Ctrl+C round trip can take tens of milliseconds waiting on the
        # target app; hand it off immediately so the listener thread stays
        # free, same reasoning as the dictation hotkey's release handoff.
        threading.Thread(target=self._capture_selection, args=(selection_ready,), daemon=True).start()

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
            self._overlay.set_state(OverlayState.IDLE)
            return
        self._overlay.set_state(OverlayState.THINKING)
        threading.Thread(
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
            self._overlay.set_state(OverlayState.ERROR, detail=detail)
        except TimeoutError as exc:
            run.mark_failed(exc)
            logger.error("%s timed out: %s", label, exc)
            self._overlay.set_state(OverlayState.ERROR, detail="timed out")
        except (GroqError, FlowError) as exc:
            run.mark_failed(exc)
            logger.error("%s failed: %s", label, exc)
            self._overlay.set_state(OverlayState.ERROR, detail="failed")
        except Exception as exc:  # never let a bug hang the app with the mic hot
            run.mark_failed(exc)
            logger.exception("unexpected %s failure", label)
            self._overlay.set_state(OverlayState.ERROR, detail="failed")

    def _process(
        self,
        start_frame: int,
        focused_process: str | None,
        session: ChunkStreamer | None,
        release_time: float,
        held_ms: float,
    ) -> None:
        with self._tracker.run({"source": "hotkey"}) as run:
            run.annotate(held_ms=round(held_ms, 1))
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
            self._overlay.set_state(OverlayState.IDLE)
            return

        cleaned_text = self._clean_up(transcript, run, focused_process)

        with run.stage(Stage.INJECT):
            pasted = self._inject_with_fallback(cleaned_text, run)
        self._overlay.set_state(
            OverlayState.IDLE if pasted else OverlayState.ERROR,
            detail="" if pasted else "elevated window — copied instead",
        )

        self._record_history(
            kind=KIND_DICTATION,
            text=cleaned_text,
            raw_text=transcript.text,
            focused_process=focused_process,
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
            except FlowError:
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

        with run.stage(Stage.ENCODE):
            flac_bytes = encode_flac(
                trimmed, self._config.audio.sample_rate, self._config.audio.channels
            )

        prompt_bias = self._build_prompt_bias()
        with run.stage(Stage.TRANSCRIBE):
            assert self._groq is not None
            future = self._network.submit(
                self._groq.transcribe(
                    flac_bytes, language=self._config.groq.language, prompt=prompt_bias or None
                )
            )
            result = future.result(timeout=self._config.groq.timeout_s + 5.0)

        run.annotate(
            characters=len(result.text),
            language=result.language or "",
            avg_logprob=result.avg_logprob if result.avg_logprob is not None else "",
            confident=result.confident,
        )
        if not result.text:
            return None
        return _Transcript(
            text=result.text, language=result.language, confident=result.confident
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
                assert self._groq is not None
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

    def _clean_up(self, transcript: _Transcript, run: RunRecorder, focused_process: str | None) -> str:
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
        style = style_for_process(focused_process)
        raw_text = transcript.text
        guardrail = ""
        failure = ""
        with run.stage(Stage.CLEANUP):
            skip_reason = self._cleanup_skip_reason(transcript, style)
            if skip_reason:
                cleaned_text = raw_text
            else:
                try:
                    cleaned_text, guardrail = self._request_cleanup(raw_text, style, transcript)
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
        )
        return cleaned_text

    def _cleanup_skip_reason(self, transcript: _Transcript, style: CleanupStyle) -> str:
        """Why this transcript bypasses the LLM, or ``""`` to run cleanup."""
        cfg = self._config.cleanup
        if not cfg.enabled:
            return "disabled"
        if style is CleanupStyle.VERBATIM:
            return "verbatim_profile"
        if cfg.skip_when_unconfident and not transcript.confident:
            return "low_confidence"
        if should_skip_cleanup(
            transcript.text, language=transcript.language, min_words=cfg.skip_min_words
        ):
            return "short_and_clean"
        return ""

    def _request_cleanup(
        self, raw_text: str, style: CleanupStyle, transcript: _Transcript
    ) -> tuple[str, str]:
        """The cleanup round trip plus its output checks. Returns the text to
        paste and the guardrail reason that rejected the rewrite (``""`` when
        the rewrite was accepted).
        """
        cfg = self._config.cleanup
        assert self._groq is not None
        future = self._network.submit(
            self._groq.cleanup(
                raw_text,
                system_prompt=system_prompt_for_style(style, language=transcript.language),
                model=cfg.model,
                temperature=cfg.temperature,
                reasoning_effort=cfg.reasoning_effort or None,
                max_output_tokens=cfg.max_output_tokens or None,
            )
        )
        result = future.result(timeout=self._config.groq.timeout_s + 5.0)
        candidate = normalize_cleanup_output(result.text, raw_text)
        if not candidate:
            return raw_text, "empty"
        if not is_cleanup_output_safe(raw_text, candidate):
            reason = unsafe_reason(raw_text, candidate)
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
            self._overlay.set_state(OverlayState.ERROR, detail="nothing selected")
            return

        instruction = self._transcribe_single_shot(start_frame, run)
        if not instruction:
            self._overlay.set_state(OverlayState.IDLE)
            return

        with run.stage(Stage.COMMAND):
            assert self._groq is not None
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
        self._overlay.set_state(
            OverlayState.IDLE if pasted else OverlayState.ERROR,
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

    # -- history (Phase 5) -------------------------------------------------

    def _record_history(
        self,
        *,
        kind: str,
        text: str,
        raw_text: str,
        instruction: str | None = None,
        focused_process: str | None = None,
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
            )
        except HistoryStoreError as exc:
            logger.warning("could not record history entry: %s", exc)


__all__ = ["Engine", "should_process"]
