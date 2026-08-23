"""Carries an existing install across the Flow -> Sway rename.

The product name is not only a label: it names the data directory, the
launch-at-login registry value, and the Credential Manager entry holding the
user's API key. Renaming without this module would, in one step, lose someone
their dictation history, their personal dictionary, their latency data, their
config, and their API key -- while leaving a stale ``Run`` entry pointing at
an executable that no longer exists.

Three rules hold throughout:

* **Idempotent.** Every step checks whether it has already happened. Running
  twice is a no-op; running on a fresh machine with no legacy install is a
  no-op.
* **Never destructive on the legacy side except where a leftover is actively
  harmful.** The data directory is *moved* (there is no value in two copies
  of a database, and a stale one would confuse a later diagnosis), but the
  old credential is *left alone* -- silently deleting somebody's stored
  secret is not ours to do. The old ``Run`` value *is* removed, because
  leaving it means the app tries to start twice at login, once from a path
  that no longer exists.
* **Best-effort.** Any single step failing is logged and skipped. A failed
  migration must never stop the app from starting; the worst case is that the
  user re-enters a key.

Called once from :func:`sway.cli.main` before configuration is resolved,
because the data directory it moves is where configuration is read from.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sway import APP_NAME

logger = logging.getLogger(__name__)

#: What the app was called before. Used to locate the legacy data directory
#: and the legacy registry value; never written anywhere new.
LEGACY_APP_NAME = "Flow"

#: The rotating log file was the only branded filename inside the data
#: directory; the databases are all generically named and carry over as-is.
LEGACY_LOG_NAME = "flow.log"
LOG_NAME = "sway.log"


@dataclass
class MigrationReport:
    """What actually happened, for logging and for tests."""

    data_dir_moved: bool = False
    log_renamed: bool = False
    key_copied: bool = False
    run_key_moved: bool = False
    steps_failed: list[str] = field(default_factory=list)

    @property
    def anything_happened(self) -> bool:
        return bool(
            self.data_dir_moved
            or self.log_renamed
            or self.key_copied
            or self.run_key_moved
        )


def migrate(*, data_dir: Path, legacy_data_dir: Path | None = None) -> MigrationReport:
    """Run every migration step. Never raises.

    ``data_dir`` is where Sway will read from; ``legacy_data_dir`` defaults to
    the sibling directory the old name would have produced, which is how the
    real call site resolves it.
    """
    report = MigrationReport()
    legacy = legacy_data_dir if legacy_data_dir is not None else _legacy_dir_for(data_dir)

    for name, step in (
        ("data directory", lambda: _move_data_dir(legacy, data_dir, report)),
        ("log file", lambda: _rename_log(data_dir, report)),
        ("API key", lambda: _copy_key(report)),
        ("launch at login", lambda: _move_run_key(report)),
    ):
        try:
            step()
        except Exception:
            logger.warning("could not migrate the %s", name, exc_info=True)
            report.steps_failed.append(name)

    if report.anything_happened:
        logger.info(
            "migrated from %s: data_dir=%s log=%s key=%s run_key=%s",
            LEGACY_APP_NAME,
            report.data_dir_moved,
            report.log_renamed,
            report.key_copied,
            report.run_key_moved,
        )
    return report


def _legacy_dir_for(data_dir: Path) -> Path:
    """Where the old build kept its data, given where the new one will.

    Both are ``<parent>/<app name>``, so the legacy path is the sibling with
    the old name. Deriving it rather than re-querying the platform keeps this
    correct when ``SWAY_DATA_DIR`` has moved the directory somewhere custom --
    in which case the sibling will simply not exist and the move is skipped.
    """
    return data_dir.parent / LEGACY_APP_NAME


def _move_data_dir(legacy: Path, target: Path, report: MigrationReport) -> None:
    """Move the whole directory, but only into empty space.

    Refusing when the target already has content is what makes this safe to
    run repeatedly: after the first migration the target exists, and a second
    run must not merge a stale legacy directory back over live data.
    """
    if not legacy.is_dir() or legacy.resolve() == target.resolve():
        return
    if target.exists() and any(target.iterdir()):
        logger.debug("target data directory already has content; leaving %s alone", legacy)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Present but empty -- created by an earlier partial start. Remove it
        # so the move is a rename rather than a merge.
        target.rmdir()

    try:
        legacy.rename(target)
    except OSError:
        # Different volume, or a file still open. Copy then remove, which is
        # slower but works across volumes.
        shutil.copytree(legacy, target)
        shutil.rmtree(legacy, ignore_errors=True)

    report.data_dir_moved = True
    logger.info("moved %s to %s", legacy, target)


def _rename_log(data_dir: Path, report: MigrationReport) -> None:
    """Fold the old log into the new one's rotation.

    Not a plain ``flow.log`` -> ``sway.log`` rename, because by the time this
    runs ``sway.log`` always exists: logging is configured before the
    migration precisely so the migration itself is logged. A rename would
    therefore never fire, and the old file would sit in the data directory
    forever.

    Instead it takes the first rotation slot, which is where a previous
    session's log belongs anyway -- ``RotatingFileHandler`` already owns
    ``.1`` through ``.3`` and will age it out on its own.
    """
    legacy = data_dir / LEGACY_LOG_NAME
    if not legacy.is_file():
        return
    target = data_dir / LOG_NAME
    if not target.exists():
        # Nothing has logged yet this run, so the simple rename is right.
        legacy.rename(target)
        report.log_renamed = True
        return

    rotated = data_dir / f"{LOG_NAME}.1"
    if rotated.exists():
        # The rotation is already full; the old log has no claim over a real
        # previous session, so drop it rather than inventing a slot.
        legacy.unlink()
    else:
        legacy.rename(rotated)
    report.log_renamed = True


def _copy_key(report: MigrationReport) -> None:
    """Copy the stored API key to the new Credential Manager entry.

    The old entry is deliberately left in place. It is the user's secret, in
    the OS's own store, and deleting it silently is not something an upgrade
    should do -- they can remove it themselves once they are satisfied.
    """
    from sway import credentials

    if not credentials.keyring_available():
        return
    if credentials.read_stored_key():
        return  # already migrated, or the user has since set a key

    legacy_key = credentials.read_legacy_key()
    if not legacy_key:
        return

    credentials.store_key(legacy_key)
    report.key_copied = True
    logger.info(
        "copied the stored API key to the %r credential; the old %r entry was left in place",
        credentials.SERVICE_NAME,
        credentials.LEGACY_SERVICE_NAME,
    )


def _move_run_key(report: MigrationReport) -> None:
    """Re-home launch-at-login, and remove the entry pointing at the old exe.

    Order matters: the new value is written first, so a failure between the
    two leaves the user with launch-at-login still working (twice, briefly)
    rather than not at all.
    """
    from sway.platform import get_platform

    platform = get_platform()
    if not platform.launches_at_login(LEGACY_APP_NAME):
        return
    if not platform.launches_at_login(APP_NAME):
        from sway.cli import startup_command

        platform.set_launch_at_login(APP_NAME, startup_command())
    platform.set_launch_at_login(LEGACY_APP_NAME, None)
    report.run_key_moved = True


__all__ = [
    "LEGACY_APP_NAME",
    "LEGACY_LOG_NAME",
    "LOG_NAME",
    "MigrationReport",
    "migrate",
]
