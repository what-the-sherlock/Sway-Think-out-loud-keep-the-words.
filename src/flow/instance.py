"""The running instance's advertisement, and how a second launch reaches it.

D1: launching Flow twice must focus the copy that is already running, not
start a rival hotkey hook that pastes everything twice. The named mutex in
:mod:`flow.gui` already *detects* the second launch; this module is how the
second process then talks to the first.

The mechanism reuses what already exists rather than adding a named pipe: the
running instance owns a loopback HTTP server on an ephemeral port
(:mod:`flow.dashboard.server`), so it writes that port and its per-launch
token to ``<data_dir>/instance.json`` and the second launch POSTs a focus
request to it.

That file holds a **live credential**, so it is not an ordinary state file:

* It lives in ``%LOCALAPPDATA%\\Flow``, which Windows already ACLs to the
  owning user; the file is additionally created ``0o600``. Note honestly that
  ``os.chmod`` is close to a no-op for real Windows ACLs -- the directory's
  inherited ACL is what actually protects this, and the mode is belt and
  braces for the POSIX adapter.
* It is rewritten, never appended, and removed on clean shutdown.
* **A stale file is treated as absent.** A port recorded by a process that
  has since died may belong to something entirely unrelated by the time it is
  read, so the recorded PID must still be alive before anything is sent to
  it.
* **A hung first instance must not hang the second one.** The focus request
  uses a short timeout and reports the difference between "no instance",
  "focused it", and "it is there but not answering" -- the caller decides
  what to tell the user, and never silently kills anything.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

INSTANCE_FILENAME = "instance.json"

#: How long the second launch waits for the first to answer. Deliberately
#: short: this runs before any window appears, so every second here is a
#: second of the user double-clicking an icon and seeing nothing at all.
FOCUS_TIMEOUT_S = 1.0


class FocusResult(StrEnum):
    """What happened when a second launch tried to reach the first."""

    #: No usable advertisement: no file, malformed, or the owner is gone.
    NO_INSTANCE = "no_instance"
    #: The running instance acknowledged and brought itself forward.
    FOCUSED = "focused"
    #: The file is valid and the process is alive, but it did not answer.
    UNRESPONSIVE = "unresponsive"


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    """What a running instance advertises about how to reach it."""

    pid: int
    port: int
    token: str
    url: str


def instance_path(data_dir: Path) -> Path:
    return Path(data_dir) / INSTANCE_FILENAME


def publish(data_dir: Path, *, port: int, token: str, url: str) -> Path | None:
    """Advertise this process. Returns the path written, or ``None`` on failure.

    Never raises: failing to advertise costs the second-launch focus nicety,
    which is not worth taking the app down for.
    """
    target = instance_path(data_dir)
    payload = {"pid": os.getpid(), "port": port, "token": token, "url": url}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Truncate-and-write rather than append, and 0o600 so the POSIX
        # adapter is not relying on directory permissions alone.
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as exc:
        logger.warning("could not publish the instance file (%s); continuing", exc)
        return None
    return target


def withdraw(data_dir: Path) -> None:
    """Remove this process's advertisement. Never raises."""
    try:
        instance_path(data_dir).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove the instance file (%s)", exc)


def read(data_dir: Path, *, is_running: object = None) -> InstanceInfo | None:
    """Read a *live* advertisement, or ``None``.

    ``is_running`` is a ``Callable[[int], bool]`` used to test whether the
    recorded PID is still alive; it is injectable so this is testable without
    spawning processes. When omitted, the platform adapter's implementation is
    used.

    Returns ``None`` -- deliberately indistinguishable from "no file" -- for a
    malformed file or a dead owner. A stale port is not merely useless, it is
    dangerous: the OS may have reassigned it to an unrelated process, and
    sending it our token would leak a credential to whatever now holds it.
    """
    target = instance_path(data_dir)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    try:
        pid = int(raw["pid"])
        port = int(raw["port"])
        token = str(raw["token"])
        url = str(raw["url"])
    except (KeyError, TypeError, ValueError):
        logger.debug("instance file at %s is malformed; ignoring", target)
        return None
    if not token or port <= 0:
        return None

    alive = is_running if callable(is_running) else _default_is_running
    try:
        if not alive(pid):
            logger.debug("instance file names pid %d, which is gone; ignoring", pid)
            return None
    except Exception:  # pragma: no cover - defensive; liveness must not throw
        logger.debug("could not test whether pid %d is alive; ignoring", pid, exc_info=True)
        return None

    return InstanceInfo(pid=pid, port=port, token=token, url=url)


def request_focus(info: InstanceInfo, *, timeout_s: float = FOCUS_TIMEOUT_S) -> FocusResult:
    """Ask a running instance to bring itself forward.

    Uses ``urllib`` rather than httpx: this runs in the second process before
    anything else is initialised, and pulling in the async HTTP stack to send
    one loopback POST would slow down the path whose entire job is to be
    invisible.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{info.port}/api/focus",
        data=b"",
        method="POST",
        headers={
            "X-Flow-Token": info.token,
            # The server rejects cross-origin state changes; name ourselves
            # honestly rather than forging a browser origin.
            "Origin": f"http://127.0.0.1:{info.port}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if 200 <= response.status < 300:
                return FocusResult.FOCUSED
            logger.debug("focus request returned HTTP %s", response.status)
    except urllib.error.HTTPError as exc:
        logger.debug("focus request rejected: HTTP %s", exc.code)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("focus request did not complete: %s", exc)
        return FocusResult.UNRESPONSIVE
    return FocusResult.UNRESPONSIVE


def _default_is_running(pid: int) -> bool:
    from flow.platform import get_platform

    return get_platform().process_is_running(pid)


__all__ = [
    "FOCUS_TIMEOUT_S",
    "INSTANCE_FILENAME",
    "FocusResult",
    "InstanceInfo",
    "instance_path",
    "publish",
    "read",
    "request_focus",
    "withdraw",
]
