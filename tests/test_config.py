"""Tests for configuration loading, layering, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from flow.config import Secret, load_config, parse_env_file
from flow.errors import ConfigError


# -- .env parsing ----------------------------------------------------------


def test_parse_env_file_handles_comments_blanks_and_export():
    text = "\n".join(
        [
            "# a comment",
            "",
            "PLAIN=value",
            "export EXPORTED=other",
            "  SPACED  =  padded  ",
        ]
    )
    assert parse_env_file(text) == {
        "PLAIN": "value",
        "EXPORTED": "other",
        "SPACED": "padded",
    }


def test_parse_env_file_handles_quotes_and_inline_comments():
    text = "\n".join(
        [
            'DOUBLE="keep # this"',
            "SINGLE='also # this'",
            "BARE=drop # this",
            "HASH_IN_VALUE=gsk_abc#def",
        ]
    )
    parsed = parse_env_file(text)
    assert parsed["DOUBLE"] == "keep # this"
    assert parsed["SINGLE"] == "also # this"
    assert parsed["BARE"] == "drop"
    # Only " #" (space-hash) starts a comment, so keys containing '#' survive.
    assert parsed["HASH_IN_VALUE"] == "gsk_abc#def"


def test_parse_env_file_ignores_non_assignments():
    assert parse_env_file("no equals sign here\n=novalue\nOK=1") == {"OK": "1"}


def test_parse_env_file_keeps_equals_in_value():
    assert parse_env_file("KEY=a=b=c") == {"KEY": "a=b=c"}


# -- layering --------------------------------------------------------------


def test_defaults_apply_with_no_file(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    assert config.data_dir == tmp_path.resolve()
    assert config.logging.level == "INFO"
    assert config.latency.enabled is True
    assert config.latency.retention_days == 30
    assert config.latency_db_path == tmp_path.resolve() / "latency.db"
    assert config.source_file is None


def test_toml_values_override_defaults(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[logging]",
                'level = "DEBUG"',
                "file_enabled = false",
                "",
                "[latency]",
                "retention_days = 7",
                "queue_size = 8",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(
        config_file=config_file,
        env={"FLOW_DATA_DIR": str(tmp_path)},
        env_file=tmp_path / "missing.env",
    )
    assert config.logging.level == "DEBUG"
    assert config.logging.file_enabled is False
    assert config.latency.retention_days == 7
    assert config.latency.queue_size == 8
    assert config.source_file == config_file


def test_env_overrides_toml(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[logging]\nlevel = "DEBUG"\n', encoding="utf-8")
    config = load_config(
        config_file=config_file,
        env={
            "FLOW_DATA_DIR": str(tmp_path),
            "FLOW_LOGGING_LEVEL": "WARNING",
            "FLOW_LATENCY_ENABLED": "no",
        },
        env_file=tmp_path / "missing.env",
    )
    assert config.logging.level == "WARNING"
    assert config.latency.enabled is False


def test_shell_env_wins_over_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")
    config = load_config(
        env={"FLOW_DATA_DIR": str(tmp_path), "GROQ_API_KEY": "from-shell"},
        env_file=env_file,
    )
    assert config.groq_api_key.reveal() == "from-shell"


def test_env_file_supplies_missing_key(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=env_file)
    assert config.groq_api_key.reveal() == "from-file"


def test_db_path_override(tmp_path: Path):
    target = tmp_path / "custom" / "timings.db"
    config = load_config(
        env={"FLOW_DATA_DIR": str(tmp_path), "FLOW_LATENCY_DB_PATH": str(target)},
        env_file=tmp_path / "missing.env",
    )
    assert config.latency_db_path == target


# -- validation ------------------------------------------------------------


def test_unknown_section_key_is_rejected(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[latency]\nretention_dayz = 7\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(
            config_file=config_file,
            env={"FLOW_DATA_DIR": str(tmp_path)},
            env_file=tmp_path / "missing.env",
        )


def test_unknown_top_level_key_is_rejected(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("nonexistent_key = 'right ctrl'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown top-level key"):
        load_config(
            config_file=config_file,
            env={"FLOW_DATA_DIR": str(tmp_path)},
            env_file=tmp_path / "missing.env",
        )


def test_missing_explicit_config_file_is_an_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(config_file=tmp_path / "nope.toml", env={}, env_file=tmp_path / "missing.env")


def test_invalid_toml_is_reported(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[latency\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(
            config_file=config_file,
            env={"FLOW_DATA_DIR": str(tmp_path)},
            env_file=tmp_path / "missing.env",
        )


@pytest.mark.parametrize(
    "env_key,value,message",
    [
        ("FLOW_LATENCY_ENABLED", "maybe", "boolean"),
        ("FLOW_LATENCY_RETENTION_DAYS", "lots", "integer"),
        ("FLOW_LOGGING_LEVEL", "CHATTY", "unknown level"),
        ("FLOW_LATENCY_QUEUE_SIZE", "0", "queue_size"),
        ("FLOW_LATENCY_BATCH_SIZE", "0", "batch_size"),
        ("FLOW_LATENCY_RETENTION_DAYS", "-1", "retention_days"),
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, env_key: str, value: str, message: str):
    with pytest.raises(ConfigError, match=message):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), env_key: value},
            env_file=tmp_path / "missing.env",
        )


def test_boolean_where_integer_expected_is_rejected(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[latency]\nretention_days = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="expected an integer"):
        load_config(
            config_file=config_file,
            env={"FLOW_DATA_DIR": str(tmp_path)},
            env_file=tmp_path / "missing.env",
        )


# -- secrets ---------------------------------------------------------------


def test_secret_never_renders_its_value():
    secret = Secret("gsk_supersecret")
    assert "gsk_supersecret" not in repr(secret)
    assert "gsk_supersecret" not in str(secret)
    assert "gsk_supersecret" not in f"{secret}"
    assert secret.reveal() == "gsk_supersecret"
    assert bool(secret) is True
    assert bool(Secret("")) is False


def test_redacted_dict_omits_the_key(tmp_path: Path):
    config = load_config(
        env={"FLOW_DATA_DIR": str(tmp_path), "GROQ_API_KEY": "gsk_supersecret"},
        env_file=tmp_path / "missing.env",
    )
    rendered = repr(config.redacted_dict())
    assert "gsk_supersecret" not in rendered
    assert config.redacted_dict()["groq_api_key"] == "set"


# -- Phase 1 sections: audio / hotkey / groq / paste / overlay -------------


def test_phase1_section_defaults(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    assert config.audio.device is None
    assert config.audio.sample_rate == 16000
    assert config.audio.channels == 1
    assert config.audio.ring_seconds == 60
    assert config.audio.vad_mode == 2
    assert config.hotkey.key == "ctrl_r"
    assert config.hotkey.min_hold_ms == 200
    assert config.groq.base_url == "https://api.groq.com"
    assert config.groq.max_retries == 1
    assert config.groq.language is None
    assert config.paste.restore_delay_ms == 150
    assert config.overlay.enabled is True


def test_phase1_sections_are_overridable_via_toml(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[audio]",
                'device = "USB Microphone"',
                "vad_mode = 3",
                "",
                "[hotkey]",
                'key = "f13"',
                "min_hold_ms = 100",
                "",
                "[groq]",
                'language = "en"',
                "max_retries = 3",
                "",
                "[paste]",
                "restore_delay_ms = 300",
                "",
                "[overlay]",
                "enabled = false",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(
        config_file=config_file,
        env={"FLOW_DATA_DIR": str(tmp_path)},
        env_file=tmp_path / "missing.env",
    )
    assert config.audio.device == "USB Microphone"
    assert config.audio.vad_mode == 3
    assert config.hotkey.key == "f13"
    assert config.hotkey.min_hold_ms == 100
    assert config.groq.language == "en"
    assert config.groq.max_retries == 3
    assert config.paste.restore_delay_ms == 300
    assert config.overlay.enabled is False


def test_phase1_sections_are_overridable_via_env(tmp_path: Path):
    config = load_config(
        env={
            "FLOW_DATA_DIR": str(tmp_path),
            "FLOW_HOTKEY_KEY": "alt_r",
            "FLOW_GROQ_LANGUAGE": "fr",
            "FLOW_OVERLAY_ENABLED": "0",
        },
        env_file=tmp_path / "missing.env",
    )
    assert config.hotkey.key == "alt_r"
    assert config.groq.language == "fr"
    assert config.overlay.enabled is False


@pytest.mark.parametrize(
    "env_key,value,message",
    [
        ("FLOW_AUDIO_CHANNELS", "2", "channels must be 1"),
        ("FLOW_AUDIO_SAMPLE_RATE", "44100", "sample_rate must be one of"),
        ("FLOW_AUDIO_RING_SECONDS", "0", "ring_seconds"),
        ("FLOW_AUDIO_VAD_MODE", "4", "vad_mode"),
        ("FLOW_AUDIO_VAD_FRAME_MS", "25", "vad_frame_ms"),
        ("FLOW_HOTKEY_KEY", "space", "hotkey].key"),
        ("FLOW_HOTKEY_MIN_HOLD_MS", "-1", "min_hold_ms"),
        ("FLOW_GROQ_TIMEOUT_S", "0", "timeout_s"),
        ("FLOW_GROQ_MAX_RETRIES", "-1", "max_retries"),
        ("FLOW_PASTE_RESTORE_DELAY_MS", "-1", "restore_delay_ms"),
    ],
)
def test_phase1_invalid_values_are_rejected(tmp_path: Path, env_key: str, value: str, message: str):
    with pytest.raises(ConfigError, match=message):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), env_key: value},
            env_file=tmp_path / "missing.env",
        )


def test_blank_groq_language_coerces_to_unset(tmp_path: Path):
    # `str | None` coercion treats a blank value as "not provided", the same
    # as every other Optional field; it never reaches the blank-string guard
    # in _validate (which exists for Config objects built directly in code).
    config_file = tmp_path / "config.toml"
    config_file.write_text('[groq]\nlanguage = "   "\n', encoding="utf-8")
    config = load_config(
        config_file=config_file,
        env={"FLOW_DATA_DIR": str(tmp_path)},
        env_file=tmp_path / "missing.env",
    )
    assert config.groq.language is None


def test_redacted_dict_includes_phase1_sections(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    redacted = config.redacted_dict()
    assert redacted["audio"]["sample_rate"] == 16000
    assert redacted["hotkey"]["key"] == "ctrl_r"
    assert redacted["groq"]["base_url"] == "https://api.groq.com"
    assert redacted["paste"]["restore_delay_ms"] == 150
    assert redacted["overlay"]["enabled"] is True


# -- Phase 4: [streaming] ----------------------------------------------------


def test_streaming_section_defaults(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    # Off by default: Groq's ASR latency is flat in clip length, so chunking
    # removes almost no tail latency while multiplying free-tier audio
    # requests. See StreamingConfig's docstring for the measurement.
    assert config.streaming.enabled is False
    assert config.streaming.chunk_seconds == 5.0
    assert config.streaming.overlap_seconds == 1.0
    assert config.streaming.poll_interval_s == 0.2


def test_streaming_section_is_overridable_via_toml(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[streaming]",
                "enabled = true",
                "chunk_seconds = 8.0",
                "overlap_seconds = 2.0",
                "poll_interval_s = 0.1",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(
        config_file=config_file,
        env={"FLOW_DATA_DIR": str(tmp_path)},
        env_file=tmp_path / "missing.env",
    )
    assert config.streaming.enabled is True
    assert config.streaming.chunk_seconds == 8.0
    assert config.streaming.overlap_seconds == 2.0
    assert config.streaming.poll_interval_s == 0.1


def test_streaming_section_is_overridable_via_env(tmp_path: Path):
    config = load_config(
        env={
            "FLOW_DATA_DIR": str(tmp_path),
            "FLOW_STREAMING_ENABLED": "1",
            "FLOW_STREAMING_CHUNK_SECONDS": "4.5",
        },
        env_file=tmp_path / "missing.env",
    )
    assert config.streaming.enabled is True
    assert config.streaming.chunk_seconds == 4.5


@pytest.mark.parametrize(
    "env_key,value,message",
    [
        ("FLOW_STREAMING_CHUNK_SECONDS", "0", "chunk_seconds must be > 0"),
        ("FLOW_STREAMING_CHUNK_SECONDS", "-1", "chunk_seconds must be > 0"),
        ("FLOW_STREAMING_OVERLAP_SECONDS", "-1", "overlap_seconds must be >= 0"),
        ("FLOW_STREAMING_POLL_INTERVAL_S", "0", "poll_interval_s must be > 0"),
    ],
)
def test_streaming_invalid_values_are_rejected(
    tmp_path: Path, env_key: str, value: str, message: str
):
    with pytest.raises(ConfigError, match=message):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), env_key: value},
            env_file=tmp_path / "missing.env",
        )


def test_streaming_overlap_must_be_less_than_chunk_seconds(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[streaming]\nchunk_seconds = 3.0\noverlap_seconds = 3.0\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="overlap_seconds must be less than chunk_seconds"):
        load_config(
            config_file=config_file,
            env={"FLOW_DATA_DIR": str(tmp_path)},
            env_file=tmp_path / "missing.env",
        )


def test_redacted_dict_includes_streaming_section(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    redacted = config.redacted_dict()
    assert redacted["streaming"]["chunk_seconds"] == 5.0
    assert redacted["streaming"]["overlap_seconds"] == 1.0


# -- Phase 5: [hotkey].command_key, [commands], [history], [tray] -----------


def test_command_key_defaults_to_shift_r(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    assert config.hotkey.command_key == "shift_r"


def test_command_key_can_be_disabled_via_blank_toml_value(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[hotkey]\ncommand_key = ""\n', encoding="utf-8")
    config = load_config(
        config_file=config_file,
        env={"FLOW_DATA_DIR": str(tmp_path)},
        env_file=tmp_path / "missing.env",
    )
    assert config.hotkey.command_key is None


def test_command_key_can_be_overridden(tmp_path: Path):
    config = load_config(
        env={"FLOW_DATA_DIR": str(tmp_path), "FLOW_HOTKEY_COMMAND_KEY": "alt_r"},
        env_file=tmp_path / "missing.env",
    )
    assert config.hotkey.command_key == "alt_r"


def test_command_key_must_be_a_known_hotkey(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"command_key must be one of"):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), "FLOW_HOTKEY_COMMAND_KEY": "space"},
            env_file=tmp_path / "missing.env",
        )


def test_command_key_must_differ_from_hotkey_key(tmp_path: Path):
    with pytest.raises(ConfigError, match="command_key must differ from"):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), "FLOW_HOTKEY_COMMAND_KEY": "ctrl_r"},
            env_file=tmp_path / "missing.env",
        )


def test_commands_section_defaults(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    assert config.commands.model == "openai/gpt-oss-20b"
    assert config.commands.temperature == 0.2
    assert config.commands.selection_timeout_s == 0.3


def test_commands_section_is_overridable_via_toml(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[commands]\nmodel = "other-model"\ntemperature = 0.5\nselection_timeout_s = 1.0\n',
        encoding="utf-8",
    )
    config = load_config(
        config_file=config_file,
        env={"FLOW_DATA_DIR": str(tmp_path)},
        env_file=tmp_path / "missing.env",
    )
    assert config.commands.model == "other-model"
    assert config.commands.temperature == 0.5
    assert config.commands.selection_timeout_s == 1.0


@pytest.mark.parametrize(
    "env_key,value,message",
    [
        ("FLOW_COMMANDS_TEMPERATURE", "3.0", "temperature must be between"),
        ("FLOW_COMMANDS_SELECTION_TIMEOUT_S", "0", "selection_timeout_s must be > 0"),
    ],
)
def test_commands_invalid_values_are_rejected(tmp_path: Path, env_key: str, value: str, message: str):
    with pytest.raises(ConfigError, match=message):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), env_key: value},
            env_file=tmp_path / "missing.env",
        )


def test_history_section_defaults(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    assert config.history.enabled is True
    assert config.history.retention_days == 90
    assert config.history_db_path == tmp_path.resolve() / "history.db"


def test_history_section_is_overridable_via_env(tmp_path: Path):
    config = load_config(
        env={
            "FLOW_DATA_DIR": str(tmp_path),
            "FLOW_HISTORY_ENABLED": "0",
            "FLOW_HISTORY_RETENTION_DAYS": "30",
        },
        env_file=tmp_path / "missing.env",
    )
    assert config.history.enabled is False
    assert config.history.retention_days == 30


def test_history_negative_retention_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="retention_days must be >= 0"):
        load_config(
            env={"FLOW_DATA_DIR": str(tmp_path), "FLOW_HISTORY_RETENTION_DAYS": "-1"},
            env_file=tmp_path / "missing.env",
        )


def test_tray_section_defaults(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    assert config.tray.enabled is True


def test_tray_can_be_disabled(tmp_path: Path):
    config = load_config(
        env={"FLOW_DATA_DIR": str(tmp_path), "FLOW_TRAY_ENABLED": "0"},
        env_file=tmp_path / "missing.env",
    )
    assert config.tray.enabled is False


def test_redacted_dict_includes_phase5_sections(tmp_path: Path):
    config = load_config(env={"FLOW_DATA_DIR": str(tmp_path)}, env_file=tmp_path / "missing.env")
    redacted = config.redacted_dict()
    assert redacted["hotkey"]["command_key"] == "shift_r"
    assert redacted["commands"]["model"] == "openai/gpt-oss-20b"
    assert redacted["history"]["retention_days"] == 90
    assert redacted["tray"]["enabled"] is True
    assert redacted["history_db_path"] == str(tmp_path.resolve() / "history.db")
