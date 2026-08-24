"""Settings store: validation, corruption recovery, and atomic persistence.

Per CLAUDE.md these target the pure functions and the store's contract, not
widgets. The cases that matter are the ones D5 calls out: a corrupt file must
never brick startup, and a bad value must cost one field rather than the
whole file.
"""

from __future__ import annotations

import json

import pytest

from sway.settings import (
    DEFAULTS,
    MAX_TRIGGERS,
    AppRule,
    Settings,
    SettingsStore,
    Trigger,
    apply_to_config,
    normalise_phrase,
    parse,
    to_dict,
)
from sway.transforms import Transform


# -- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("My Email.", "my email"),
        ("  my   email  ", "my email"),
        ("MY EMAIL!?", "my email"),
        ("“my email”", "my email"),
        ("", ""),
        ("...", ""),
    ],
)
def test_normalise_phrase(raw, expected):
    assert normalise_phrase(raw) == expected


# -- parsing ---------------------------------------------------------------


def test_parse_empty_gives_defaults():
    assert parse({}) == DEFAULTS


def test_parse_rejects_non_object():
    assert parse([1, 2, 3]) == DEFAULTS
    assert parse("nope") == DEFAULTS
    assert parse(None) == DEFAULTS


def test_bad_field_costs_only_that_field():
    """The whole point: one broken value must not discard the rest."""
    result = parse(
        {
            "hotkey": "ctrl_l",
            "overlay_position": "somewhere-invented",
            "history_retention_days": "not a number",
            "launch_at_login": "yes please",
        }
    )
    assert result.hotkey == "ctrl_l"  # kept
    assert result.overlay_position == DEFAULTS.overlay_position
    assert result.history_retention_days == DEFAULTS.history_retention_days
    assert result.launch_at_login is DEFAULTS.launch_at_login


def test_retention_is_clamped_not_rejected():
    assert parse({"history_retention_days": -5}).history_retention_days == 0
    assert parse({"history_retention_days": 99_999}).history_retention_days == 3650


def test_blank_strings_become_none():
    """The UI sends "" for "use the default", which must not persist as ""."""
    result = parse({"hotkey": "   ", "audio_device": "", "language": ""})
    assert result.hotkey is None
    assert result.audio_device is None
    assert result.language is None


def test_hotkey_rejects_an_unknown_key_name():
    # Unlike the free-text fields, hotkey/command_hotkey/hands_free_key must
    # be an actual KNOWN_KEYS name -- previously this field was never
    # validated at the settings layer at all (only implicitly, later, by
    # config.py's validate() on the merged config).
    assert parse({"hotkey": "not_a_real_key"}).hotkey is None
    assert parse({"command_hotkey": "not_a_real_key"}).command_hotkey is None
    assert parse({"hands_free_key": "not_a_real_key"}).hands_free_key is None


def test_hotkey_chord_rejects_an_unparseable_shortcut():
    assert parse({"hotkey_chord": "just-a-letter"}).hotkey_chord is None
    assert parse({"command_hotkey_chord": ""}).command_hotkey_chord is None


def test_hotkey_chord_accepts_a_valid_chord_and_canonicalises_it():
    result = parse({"hotkey_chord": "Alt+Ctrl+p"})
    assert result.hotkey_chord == "ctrl+alt+p"


def test_hotkey_chord_rejects_a_reserved_chord():
    assert parse({"hands_free_chord": "ctrl+v"}).hands_free_chord is None


def test_hands_free_double_tap_defaults_to_none_not_false():
    # None means "use config's default", distinct from an explicit False --
    # unlike hands_free_enabled, this field has a config.toml equivalent.
    assert parse({}).hands_free_double_tap is None
    assert parse({"hands_free_double_tap": False}).hands_free_double_tap is False
    assert parse({"hands_free_double_tap": True}).hands_free_double_tap is True


def test_unknown_keys_survive_a_round_trip():
    """A file written by a newer build must not be gutted by an older one."""
    raw = {"hotkey": "ctrl_r", "future_feature": {"nested": True}}
    written = to_dict(parse(raw))
    assert written["future_feature"] == {"nested": True}


def test_unknown_keys_never_shadow_known_ones():
    settings = parse({"hotkey": "ctrl_r"})
    poisoned = Settings(hotkey="ctrl_l", unknown={"hotkey": "evil"})
    assert to_dict(poisoned)["hotkey"] == "ctrl_l"
    assert to_dict(settings)["hotkey"] == "ctrl_r"


# -- triggers --------------------------------------------------------------


def test_duplicate_trigger_phrases_are_dropped():
    """A second trigger on the same phrase could never fire."""
    result = parse(
        {
            "triggers": [
                {"id": "a", "phrase": "my email", "payload": "1", "type": "macro"},
                {"id": "b", "phrase": "My Email.", "payload": "2", "type": "macro"},
            ]
        }
    )
    assert [t.id for t in result.triggers] == ["a"]


def test_macro_without_payload_is_dropped():
    result = parse({"triggers": [{"phrase": "x", "type": "macro"}]})
    assert result.triggers == ()


def test_date_trigger_needs_no_payload():
    result = parse({"triggers": [{"phrase": "today", "type": "date"}]})
    assert len(result.triggers) == 1
    assert result.triggers[0].type == "date"


def test_unsupported_trigger_type_is_dropped():
    """`transform` is D8 proper; this build must not pretend to run it."""
    result = parse({"triggers": [{"phrase": "polish", "type": "transform", "payload": "x"}]})
    assert result.triggers == ()


def test_trigger_list_is_capped():
    raw = {
        "triggers": [
            {"phrase": f"phrase number {i}", "payload": "x", "type": "macro"}
            for i in range(MAX_TRIGGERS + 25)
        ]
    }
    assert len(parse(raw).triggers) == MAX_TRIGGERS


def test_triggers_get_ids_when_missing():
    result = parse({"triggers": [{"phrase": "x", "payload": "y", "type": "macro"}]})
    assert result.triggers[0].id


# -- app rules -------------------------------------------------------------


def test_app_rule_with_unknown_style_is_dropped():
    result = parse({"app_rules": [{"process": "slack.exe", "style": "shouty"}]})
    assert result.app_rules == ()


def test_duplicate_app_rules_keep_the_first():
    result = parse(
        {
            "app_rules": [
                {"process": "Slack.exe", "style": "casual"},
                {"process": "slack.exe", "style": "formal"},
            ]
        }
    )
    assert [r.style for r in result.app_rules] == ["casual"]


# -- the store -------------------------------------------------------------


def test_store_creates_nothing_until_written(tmp_path):
    store = SettingsStore.open(tmp_path)
    assert store.get() == DEFAULTS
    assert not (tmp_path / "settings.json").exists()


def test_update_persists_and_reloads(tmp_path):
    store = SettingsStore.open(tmp_path)
    store.update(hotkey="ctrl_l", history_retention_days=7)

    reopened = SettingsStore.open(tmp_path)
    assert reopened.get().hotkey == "ctrl_l"
    assert reopened.get().history_retention_days == 7


def test_update_rejects_unknown_fields(tmp_path):
    store = SettingsStore.open(tmp_path)
    with pytest.raises(ValueError, match="unknown setting"):
        store.update(not_a_real_setting=1)


def test_update_revalidates_incoming_values(tmp_path):
    """A value from the UI is held to the same rules as one read off disk."""
    store = SettingsStore.open(tmp_path)
    assert store.update(history_retention_days=999_999).history_retention_days == 3650


def test_corrupt_file_falls_back_and_is_preserved(tmp_path):
    """D5: a deliberately corrupted settings.json still starts the app."""
    target = tmp_path / "settings.json"
    target.write_text('{"hotkey": "ctrl_l", oh no', encoding="utf-8")

    store = SettingsStore.open(tmp_path)
    assert store.get() == DEFAULTS
    # The user's content is kept rather than silently destroyed.
    salvaged = list(tmp_path.glob("settings.corrupt-*.json"))
    assert len(salvaged) == 1
    assert "ctrl_l" in salvaged[0].read_text(encoding="utf-8")


def test_corrupt_file_is_replaced_cleanly_on_next_write(tmp_path):
    (tmp_path / "settings.json").write_text("{{{{", encoding="utf-8")
    store = SettingsStore.open(tmp_path)
    store.update(hotkey="ctrl_r")
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["hotkey"] == "ctrl_r"


def test_write_leaves_no_temporary_files(tmp_path):
    store = SettingsStore.open(tmp_path)
    store.update(hotkey="ctrl_r")
    store.update(hotkey="ctrl_l")
    assert not list(tmp_path.glob(".settings-*"))


def test_directory_is_created_if_absent(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    store = SettingsStore(nested / "settings.json")
    store.update(hotkey="ctrl_r")
    assert (nested / "settings.json").is_file()


# -- config overlay --------------------------------------------------------


def test_apply_to_config_only_touches_overridden_fields(make_config):
    config = make_config()
    original_sample_rate = config.audio.sample_rate

    result = apply_to_config(config, Settings(audio_device="Headset", hotkey="ctrl_l"))

    assert result.audio.device == "Headset"
    assert result.hotkey.key == "ctrl_l"
    # Everything with no UI must survive untouched.
    assert result.audio.sample_rate == original_sample_rate
    assert result.cleanup == config.cleanup


def test_apply_to_config_leaves_none_alone(make_config):
    config = make_config()
    result = apply_to_config(config, Settings())
    assert result.audio.device == config.audio.device
    assert result.hotkey.key == config.hotkey.key


def test_apply_to_config_layers_every_new_hotkey_field(make_config):
    config = make_config()
    result = apply_to_config(
        config,
        Settings(
            hotkey_chord="ctrl+alt+space",
            command_hotkey="alt_l",
            command_hotkey_chord="ctrl+alt+m",
            hands_free_key="caps_lock",
            hands_free_chord=None,
            hands_free_double_tap=True,
        ),
    )
    assert result.hotkey.key_chord == "ctrl+alt+space"
    assert result.hotkey.command_key == "alt_l"
    assert result.hotkey.command_key_chord == "ctrl+alt+m"
    assert result.hotkey.hands_free_key == "caps_lock"
    assert result.hotkey.hands_free_double_tap is True


def test_apply_to_config_leaves_hands_free_double_tap_alone_when_none(make_config):
    config = make_config()
    result = apply_to_config(config, Settings(hands_free_double_tap=None))
    assert result.hotkey.hands_free_double_tap == config.hotkey.hands_free_double_tap


def test_apply_to_config_always_sets_retention(make_config):
    """Retention has a real default rather than None, so it always applies."""
    config = make_config()
    result = apply_to_config(config, Settings(history_retention_days=5))
    assert result.history.retention_days == 5


def test_trigger_normalised_phrase_matches_module_helper():
    assert Trigger(id="a", phrase="  My Email. ", payload="x").normalised_phrase() == "my email"


def test_app_rule_normalises_process():
    assert AppRule(process="  Slack.EXE ", style="casual").normalised_process() == "slack.exe"


# -- transform output expectations, and reading back an older settings.json --


def test_transform_expectations_round_trip(tmp_path):
    """A user who ticks "produces a list" must still have it ticked tomorrow."""
    store = SettingsStore(tmp_path / "settings.json")
    custom = Transform(
        id="mine",
        name="Bulletise",
        instruction="Rewrite as a list",
        enabled=True,
        allows_markdown=True,
        may_shorten=True,
    )
    store.update(transforms=(custom,))

    reloaded = SettingsStore(tmp_path / "settings.json").load()
    assert reloaded.transforms[0].allows_markdown is True
    assert reloaded.transforms[0].may_shorten is True
    assert reloaded.transforms[0].may_lengthen is False


def test_a_settings_file_written_before_expectations_existed_keeps_them(tmp_path):
    """The migration that matters. Every settings.json in the wild stores the
    nine built-ins without these fields; reading them back as False would
    silently restore the bug they were added to fix -- "make that bullet
    points" produces bullets, the markdown guardrail rejects them, and the
    user gets plain text with no indication anything happened."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "transforms": [
                    {
                        "id": "bullets",
                        "name": "Turn into bullet points",
                        "instruction": "Rewrite as a bulleted list",
                        "phrase": "make that bullet points",
                        "enabled": True,
                        "builtin": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = SettingsStore(path).load()
    assert loaded.transforms[0].allows_markdown is True


def test_a_custom_transform_with_no_expectations_stays_strict(tmp_path):
    """Only shipped ids get a shipped default. Intent cannot be inferred from
    a free-text instruction, so anything else keeps the full guardrails."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "transforms": [
                    {"id": "mine", "name": "Mine", "instruction": "Do a thing"}
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = SettingsStore(path).load()
    assert loaded.transforms[0].allows_markdown is False
    assert loaded.transforms[0].may_shorten is False


def test_an_explicit_false_beats_the_shipped_default(tmp_path):
    """A user who unticks the box on a built-in keeps it unticked."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "transforms": [
                    {
                        "id": "bullets",
                        "name": "Turn into bullet points",
                        "instruction": "Rewrite as a bulleted list",
                        "allows_markdown": False,
                        "builtin": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert SettingsStore(path).load().transforms[0].allows_markdown is False
