"""The Flow -> Sway migration.

What these protect is a one-way door: if the migration is wrong, a user loses
their history, their dictionary and their API key on upgrade, and there is no
second chance to get it right. So every step is tested for the three cases
that matter -- it works, it is idempotent, and it tolerates a missing source.
"""

from __future__ import annotations

import pytest

from sway import credentials, migrate
from sway.migrate import LEGACY_APP_NAME, MigrationReport

#: Captured at import, before the autouse fixture below stubs it out --
#: otherwise a test re-patching "the original" would restore the stub.
_REAL_MOVE_RUN_KEY = migrate._move_run_key


@pytest.fixture
def dirs(tmp_path):
    """A parent holding both the legacy and the target data directories."""
    return tmp_path / LEGACY_APP_NAME, tmp_path / "Sway"


def _populate(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "history.db").write_text("history", encoding="utf-8")
    (path / "settings.json").write_text("{}", encoding="utf-8")
    (path / "flow.log").write_text("old log", encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_os_side_effects(monkeypatch):
    """Keep the credential and registry steps off the real machine.

    Individual tests re-enable the step they are actually about.
    """
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.setattr(migrate, "_move_run_key", lambda report: None)


# -- the data directory ----------------------------------------------------


def test_moves_the_legacy_directory(dirs):
    legacy, target = dirs
    _populate(legacy)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.data_dir_moved is True
    assert (target / "history.db").read_text(encoding="utf-8") == "history"
    assert (target / "settings.json").exists()
    assert not legacy.exists()


def test_is_a_no_op_when_there_is_nothing_to_migrate(dirs):
    """A fresh machine has never had the old build."""
    legacy, target = dirs

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.anything_happened is False
    assert report.steps_failed == []


def test_running_twice_changes_nothing(dirs):
    legacy, target = dirs
    _populate(legacy)

    first = migrate.migrate(data_dir=target, legacy_data_dir=legacy)
    second = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert first.data_dir_moved is True
    assert second.data_dir_moved is False
    assert (target / "history.db").read_text(encoding="utf-8") == "history"


def test_never_overwrites_live_data(dirs):
    """The dangerous case: both directories have content.

    A stale legacy directory must not be merged back over data the user has
    since created under the new name.
    """
    legacy, target = dirs
    _populate(legacy)
    target.mkdir(parents=True)
    (target / "history.db").write_text("NEW AND IMPORTANT", encoding="utf-8")

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.data_dir_moved is False
    assert (target / "history.db").read_text(encoding="utf-8") == "NEW AND IMPORTANT"
    # Left for the user to inspect rather than destroyed.
    assert legacy.exists()


def test_an_empty_target_is_not_treated_as_live_data(dirs):
    """A directory created by a partial start must not block the move."""
    legacy, target = dirs
    _populate(legacy)
    target.mkdir(parents=True)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.data_dir_moved is True
    assert (target / "history.db").exists()


def test_legacy_and_target_being_the_same_path_is_safe(tmp_path):
    """A custom SWAY_DATA_DIR could resolve both to one place."""
    report = migrate.migrate(data_dir=tmp_path, legacy_data_dir=tmp_path)
    assert report.data_dir_moved is False


# -- the log file ----------------------------------------------------------


def test_renames_the_log_file(dirs):
    legacy, target = dirs
    _populate(legacy)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.log_renamed is True
    assert (target / "sway.log").read_text(encoding="utf-8") == "old log"
    assert not (target / "flow.log").exists()


def test_the_old_log_is_kept_when_the_new_one_already_exists(dirs):
    """The real ordering: logging is configured *before* the migration runs,
    so sway.log always exists by the time this step happens. A plain rename
    would silently never fire and leave flow.log behind forever."""
    legacy, target = dirs
    # The state after the directory move: the old log has been carried over,
    # and this run's logging has already created its own file.
    target.mkdir(parents=True, exist_ok=True)
    (target / "flow.log").write_text("old log", encoding="utf-8")
    (target / "sway.log").write_text("this run", encoding="utf-8")

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.log_renamed is True
    assert not (target / "flow.log").exists()
    # Folded into the rotation the handler already owns.
    assert (target / "sway.log.1").read_text(encoding="utf-8") == "old log"
    assert (target / "sway.log").read_text(encoding="utf-8") == "this run"


def test_a_full_rotation_drops_the_old_log_rather_than_inventing_a_slot(dirs):
    legacy, target = dirs
    target.mkdir(parents=True, exist_ok=True)
    (target / "flow.log").write_text("old log", encoding="utf-8")
    (target / "sway.log").write_text("this run", encoding="utf-8")
    (target / "sway.log.1").write_text("last run", encoding="utf-8")

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.log_renamed is True
    assert not (target / "flow.log").exists()
    assert (target / "sway.log.1").read_text(encoding="utf-8") == "last run"


# -- the API key -----------------------------------------------------------


class FakeKeyring:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        del self.values[(service, account)]


def test_copies_the_stored_key_forward(dirs, monkeypatch):
    legacy, target = dirs
    fake = FakeKeyring({(credentials.LEGACY_SERVICE_NAME, credentials.ACCOUNT_NAME): "gsk_old"})
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.key_copied is True
    assert fake.values[(credentials.SERVICE_NAME, credentials.ACCOUNT_NAME)] == "gsk_old"


def test_leaves_the_old_credential_in_place(dirs, monkeypatch):
    """Deleting somebody else's stored secret is not an upgrade's job."""
    legacy, target = dirs
    slot = (credentials.LEGACY_SERVICE_NAME, credentials.ACCOUNT_NAME)
    fake = FakeKeyring({slot: "gsk_old"})
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)

    migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert fake.values[slot] == "gsk_old"


def test_never_overwrites_a_key_set_under_the_new_name(dirs, monkeypatch):
    legacy, target = dirs
    fake = FakeKeyring(
        {
            (credentials.LEGACY_SERVICE_NAME, credentials.ACCOUNT_NAME): "gsk_old",
            (credentials.SERVICE_NAME, credentials.ACCOUNT_NAME): "gsk_current",
        }
    )
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.key_copied is False
    assert fake.values[(credentials.SERVICE_NAME, credentials.ACCOUNT_NAME)] == "gsk_current"


def test_no_credential_store_is_not_a_failure(dirs, monkeypatch):
    legacy, target = dirs
    monkeypatch.setattr(credentials, "_keyring", lambda: None)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.key_copied is False
    assert "API key" not in report.steps_failed


# -- launch at login -------------------------------------------------------


class FakePlatform:
    def __init__(self, registered=()):
        self.values = {name: "old command" for name in registered}

    def launches_at_login(self, app_name):
        return app_name in self.values

    def set_launch_at_login(self, app_name, command):
        if command is None:
            self.values.pop(app_name, None)
        else:
            self.values[app_name] = command
        return True


def test_moves_the_run_key(dirs, monkeypatch):
    """A stale value pointing at a deleted exe must not survive."""
    legacy, target = dirs
    fake = FakePlatform(registered=[LEGACY_APP_NAME])
    monkeypatch.setattr(migrate, "_move_run_key", _REAL_MOVE_RUN_KEY)
    monkeypatch.setattr("sway.platform.get_platform", lambda: fake)
    monkeypatch.setattr("sway.cli.startup_command", lambda: "new command")

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.run_key_moved is True
    assert LEGACY_APP_NAME not in fake.values
    assert fake.values["Sway"] == "new command"


def test_run_key_untouched_when_not_registered(dirs, monkeypatch):
    legacy, target = dirs
    fake = FakePlatform()
    monkeypatch.setattr(migrate, "_move_run_key", _REAL_MOVE_RUN_KEY)
    monkeypatch.setattr("sway.platform.get_platform", lambda: fake)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert report.run_key_moved is False
    assert fake.values == {}


# -- failure isolation -----------------------------------------------------


def test_one_failing_step_does_not_stop_the_others(dirs, monkeypatch):
    """A migration problem must never be why the app will not start."""
    legacy, target = dirs
    _populate(legacy)

    def explode(report):
        raise RuntimeError("vault on fire")

    monkeypatch.setattr(migrate, "_copy_key", explode)

    report = migrate.migrate(data_dir=target, legacy_data_dir=legacy)

    assert "API key" in report.steps_failed
    # The data still moved, which is the part that cannot be redone by hand.
    assert report.data_dir_moved is True
    assert (target / "history.db").exists()


def test_report_summarises_whether_anything_happened():
    assert MigrationReport().anything_happened is False
    assert MigrationReport(key_copied=True).anything_happened is True
