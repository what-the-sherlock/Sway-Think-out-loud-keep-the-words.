"""End-to-end smoke tests for the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from sway.cli import main


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep the CLI away from the developer's real config, .env, and data dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOW_CONFIG", raising=False)
    monkeypatch.setenv("FLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FLOW_LOGGING_FILE_ENABLED", "0")


def _demo(tmp_path: Path, *extra: str) -> int:
    # 2 runs of real sleeps is ~1s; enough to prove the wiring without a slow suite.
    return main(["demo", "--runs", "2", "--seed", "1", *extra])


def test_demo_runs_end_to_end_and_prints_a_table(tmp_path: Path, capsys: pytest.CaptureFixture):
    assert _demo(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Stub pipeline latency" in out
    assert "TOTAL" in out
    assert "transcribe" in out
    assert (tmp_path / "data" / "latency.db").exists()


def test_demo_records_failures(tmp_path: Path, capsys: pytest.CaptureFixture):
    assert _demo(tmp_path, "--fail-rate", "1.0") == 0
    assert "2 run(s), 2 failed" in capsys.readouterr().out


def test_report_reads_back_what_demo_wrote(tmp_path: Path, capsys: pytest.CaptureFixture):
    assert _demo(tmp_path) == 0
    capsys.readouterr()

    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "Stage latency" in out
    assert "2 recorded overall" in out
    assert "TOTAL" in out


def test_report_without_a_database_fails_cleanly(capsys: pytest.CaptureFixture):
    assert main(["report"]) == 1
    assert "no latency database" in capsys.readouterr().err


def test_config_command_redacts_the_api_key(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecret")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "gsk_supersecret" not in out
    assert "groq_api_key = set" in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture):
    assert main([]) == 0
    assert "usage: sway" in capsys.readouterr().out


def test_invalid_config_reports_an_error(tmp_path: Path, capsys: pytest.CaptureFixture):
    (tmp_path / "config.toml").write_text("[latency]\nqueue_size = 0\n", encoding="utf-8")
    assert main(["config"]) == 1
    assert "queue_size" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["demo", "--runs", "0"], ["demo", "--fail-rate", "2"]])
def test_invalid_demo_arguments_are_rejected(args: list[str], capsys: pytest.CaptureFixture):
    assert main(args) == 2
    assert "error:" in capsys.readouterr().err


def test_config_file_is_discovered_in_the_working_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    (tmp_path / "config.toml").write_text('[logging]\nlevel = "WARNING"\n', encoding="utf-8")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "level = WARNING" in out
    assert "config.toml" in out


# -- history (Phase 5) -------------------------------------------------------


def test_history_list_without_a_database_fails_cleanly(capsys: pytest.CaptureFixture):
    assert main(["history", "list"]) == 1
    assert "no history database" in capsys.readouterr().err


def _seed_history(tmp_path: Path) -> None:
    from sway.config import load_config
    from sway.history import KIND_COMMAND, KIND_DICTATION, HistoryStore

    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path / "data")}, env_file=tmp_path / "missing.env")
    with HistoryStore.open(config.history_db_path) as store:
        store.add(kind=KIND_DICTATION, text="buy some milk", raw_text="buy some milk")
        store.add(
            kind=KIND_COMMAND,
            text="- eggs\n- bread",
            raw_text="eggs, bread",
            instruction="make this bullet points",
        )


def test_history_list_prints_recorded_entries(tmp_path: Path, capsys: pytest.CaptureFixture):
    _seed_history(tmp_path)
    assert main(["history", "list"]) == 0
    out = capsys.readouterr().out
    assert "buy some milk" in out
    assert "make this bullet points" in out


def test_history_list_search_filters_entries(tmp_path: Path, capsys: pytest.CaptureFixture):
    _seed_history(tmp_path)
    assert main(["history", "list", "--search", "milk"]) == 0
    out = capsys.readouterr().out
    assert "buy some milk" in out
    assert "eggs" not in out


def test_history_list_kind_filters_entries(tmp_path: Path, capsys: pytest.CaptureFixture):
    _seed_history(tmp_path)
    assert main(["history", "list", "--kind", "command"]) == 0
    out = capsys.readouterr().out
    assert "eggs" in out
    assert "milk" not in out


def test_history_list_invalid_last_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture):
    _seed_history(tmp_path)
    assert main(["history", "list", "--last", "0"]) == 2
    assert "error:" in capsys.readouterr().err
