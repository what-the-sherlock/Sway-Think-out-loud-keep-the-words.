"""A single background thread running one asyncio event loop for all network I/O.

CLAUDE.md: network work runs on a single asyncio loop in a dedicated thread,
not per-request loops or ad hoc threads. :class:`~flow.groq_client.GroqClient`
is constructed and used from coroutines submitted here.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

T = TypeVar("T")


class NetworkLoop:
    def __init__(self, *, name: str = "flow-network-loop") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._ready = threading.Event()
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started

    def start(self, *, timeout: float = 5.0) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("network loop did not start in time")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Schedule ``coro`` on the loop thread. Safe to call from any thread."""
        if not self._started:
            raise RuntimeError("network loop has not been started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, *, timeout: float = 5.0) -> None:
        if not self._started:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        self._started = False


__all__ = ["NetworkLoop"]
