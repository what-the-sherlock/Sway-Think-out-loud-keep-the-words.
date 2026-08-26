"""The mutation layer behind the UI's write routes.

Tested by calling the functions directly rather than over HTTP: these are
where validation lives, and the security-relevant property is that a hostile
or malformed payload is rejected here, before anything reaches a store.
"""

from __future__ import annotations

import pytest

from sway.dashboard import actions
from sway.dashboard.actions import ActionError
from sway.history import KIND_DICTATION, HistoryStore
from sway.settings import MAX_PHRASE_CHARS, SettingsStore, Trigger


@pytest.fixture
def store(tmp_path):
    return SettingsStore.open(tmp_path)


# -- settings --------------------------------------------------------------


def test_update_applies_only_the_keys_present(store):
    store.update(dictation_bindings=("ctrl_l",), launch_at_login=True)
    actions.update_settings(store, {"launch_at_login": False})
    # An absent key means "leave it alone", so two controls saved
    # concurrently cannot clobber each other.
    assert store.get().dictation_bindings == ("ctrl_l",)
    assert store.get().launch_at_login is False


def test_update_with_nothing_recognised_is_an_error(store):
    with pytest.raises(ActionError, match="nothing to change"):
        actions.update_settings(store, {"totally_made_up": 1})


def test_update_rejects_an_unknown_overlay_position(store):
    with pytest.raises(ActionError, match="unknown overlay position"):
        actions.update_settings(store, {"overlay_position": "in-the-bin"})


def test_update_rejects_a_non_boolean_toggle(store):
    with pytest.raises(ActionError, match="true or false"):
        actions.update_settings(store, {"launch_at_login": "yes"})


def test_update_rejects_out_of_range_retention(store):
    with pytest.raises(ActionError, match="between 0 and 3650"):
        actions.update_settings(store, {"history_retention_days": 10_000})


def test_empty_string_clears_an_override(store):
    """"System default" in the UI sends "", which must clear, not persist."""
    store.update(audio_device="Headset")
    actions.update_settings(store, {"audio_device": ""})
    assert store.get().audio_device is None


def test_only_allowlisted_fields_are_writable(store):
    """`triggers` is managed by its own routes and must not be settable here."""
    with pytest.raises(ActionError, match="nothing to change"):
        actions.update_settings(store, {"triggers": [{"phrase": "x", "payload": "y"}]})


# -- activation bindings -----------------------------------------------------


def test_save_activation_binding_adds_a_hold_key(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    assert store.get().dictation_bindings == ("ctrl_l",)


def test_save_activation_binding_op_defaults_to_add(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    assert store.get().dictation_bindings == ("ctrl_l",)


def test_save_activation_binding_adds_a_chord(store):
    actions.save_activation_binding(
        store, {"target": "hands_free", "op": "add", "binding": "Alt+Ctrl+Space"}
    )
    assert store.get().hands_free_bindings == ("ctrl+alt+space",)


def test_save_activation_binding_adds_a_second_binding_alongside_the_first(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl+alt+space"})
    assert store.get().dictation_bindings == ("ctrl_l", "ctrl+alt+space")


def test_save_activation_binding_add_materializes_config_defaults_first(store):
    """Adding a second binding to a never-customized target must not silently
    discard config's default -- it should end up alongside it."""
    settings = actions.save_activation_binding(
        store,
        {"target": "dictation", "binding": "ctrl+alt+space"},
        effective={"hotkey": "ctrl_r"},
    )
    assert settings.dictation_bindings == ("ctrl_r", "ctrl+alt+space")


def test_save_activation_binding_remove(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl+alt+space"})
    actions.save_activation_binding(
        store, {"target": "dictation", "op": "remove", "binding": "ctrl_l"}
    )
    assert store.get().dictation_bindings == ("ctrl+alt+space",)


def test_save_activation_binding_remove_rejects_a_binding_not_present(store):
    with pytest.raises(ActionError, match="not set"):
        actions.save_activation_binding(
            store, {"target": "dictation", "op": "remove", "binding": "ctrl_l"}
        )


def test_save_activation_binding_replace(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    actions.save_activation_binding(
        store,
        {"target": "dictation", "op": "replace", "old_binding": "ctrl_l", "binding": "ctrl_r"},
    )
    assert store.get().dictation_bindings == ("ctrl_r",)


def test_save_activation_binding_replace_of_a_missing_old_binding_just_adds(store):
    actions.save_activation_binding(
        store,
        {"target": "dictation", "op": "replace", "old_binding": "ctrl_l", "binding": "ctrl_r"},
    )
    assert store.get().dictation_bindings == ("ctrl_r",)


def test_save_activation_binding_rejects_a_duplicate_add(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    with pytest.raises(ActionError, match="already set"):
        actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})


def test_save_activation_binding_rejects_an_unknown_target(store):
    with pytest.raises(ActionError, match="unknown activation target"):
        actions.save_activation_binding(store, {"target": "nope", "binding": "ctrl_l"})


def test_save_activation_binding_rejects_an_unusable_binding(store):
    with pytest.raises(ActionError):
        actions.save_activation_binding(store, {"target": "command", "binding": "just-a-letter"})


def test_save_activation_binding_rejects_an_unknown_op(store):
    with pytest.raises(ActionError, match="unknown operation"):
        actions.save_activation_binding(store, {"target": "dictation", "op": "nope", "binding": "ctrl_l"})


def test_save_activation_binding_hard_rejects_a_hold_key_collision(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_r"})
    with pytest.raises(ActionError, match="already"):
        actions.save_activation_binding(store, {"target": "hands_free", "binding": "ctrl_r"})
    # The rejected save must not have partially applied.
    assert store.get().hands_free_bindings is None


def test_save_activation_binding_hard_rejects_a_hold_key_collision_against_config_default(store):
    """Even a binding never explicitly overridden in Settings is still a
    real collision if it's what the engine is actually using -- caught via
    the `effective` values (normally the engine's own describe())."""
    with pytest.raises(ActionError, match="already"):
        actions.save_activation_binding(
            store,
            {"target": "hands_free", "binding": "shift_r"},
            effective={"command_hotkey": "shift_r"},
        )


def test_save_activation_binding_hard_rejects_a_chord_collision(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl+alt+p"})
    with pytest.raises(ActionError, match="already"):
        actions.save_activation_binding(store, {"target": "command", "binding": "ctrl+alt+p"})


def test_save_activation_binding_does_not_reject_a_merely_overlapping_binding(store):
    """A binding sharing a modifier with another target's -- without being
    identical -- is not a conflict at all: sway.hotkey.BindingListener
    resolves that ambiguity itself at dispatch time (prefix grace extended
    to bare holds), so e.g. dictation on bare ctrl_r and command on
    ctrl+cmd coexist correctly. An earlier version of this hard-rejected
    such saves outright, which made half of Ctrl-space unusable for
    anything but dictation -- see sway.hotkey.needs_prefix_grace."""
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_r"})
    actions.save_activation_binding(store, {"target": "command", "binding": "ctrl+cmd"})
    assert store.get().command_bindings == ("ctrl+cmd",)


def test_save_activation_binding_does_not_reject_a_transform_collision(store):
    """A binding colliding with a *transform* shortcut is a soft warning
    shown elsewhere (activation_view's conflicts), not a hard reject here --
    mirroring how the transforms UI already treats a transform/transform
    collision as non-blocking."""
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl+alt+p"})


def test_reset_activation_binding_clears_the_whole_list(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl+alt+p"})
    actions.reset_activation_binding(store, {"target": "dictation"})
    assert store.get().dictation_bindings is None


def test_reset_activation_binding_rejects_an_unknown_target(store):
    with pytest.raises(ActionError, match="unknown activation target"):
        actions.reset_activation_binding(store, {"target": "nope"})


def test_activation_view_reports_the_default_flag(store):
    view = actions.activation_view(store.get())
    assert view["dictation"]["is_default"] is True
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl_l"})
    view = actions.activation_view(store.get())
    assert view["dictation"]["is_default"] is False


def test_activation_view_lists_effective_bindings_with_labels_and_shape(store):
    view = actions.activation_view(store.get(), effective={"hotkey": "ctrl_r"})
    assert view["dictation"]["bindings"] == [
        {"value": "ctrl_r", "label": "Right Ctrl", "shape": "hold", "advisory": ""}
    ]


def test_activation_view_reports_conflicts_symmetrically(store):
    actions.save_activation_binding(store, {"target": "dictation", "binding": "ctrl+alt+p"})
    # Bypass save_activation_binding's hard reject to inspect how the view
    # reports an existing conflict (e.g. one created by two independent
    # settings writes, which the view -- not the save path -- is what
    # surfaces to the user).
    store.update(command_bindings=("ctrl+alt+p",))
    view = actions.activation_view(store.get())
    assert "the voice-command hotkey" in view["dictation"]["conflicts"]
    assert "the dictation hotkey" in view["command"]["conflicts"]


def test_activation_view_includes_hands_free_mode_flags(store):
    view = actions.activation_view(store.get())
    assert view["hands_free"]["enabled"] is False
    assert view["hands_free"]["double_tap"] is False


# -- triggers --------------------------------------------------------------


def test_save_creates_a_trigger(store):
    result = actions.save_trigger(store, {"phrase": "my email", "payload": "a@b.co"})
    assert len(result.triggers) == 1
    assert result.triggers[0].id


def test_save_requires_a_phrase(store):
    with pytest.raises(ActionError, match="phrase"):
        actions.save_trigger(store, {"payload": "a@b.co"})


def test_macro_requires_a_payload(store):
    with pytest.raises(ActionError, match="payload"):
        actions.save_trigger(store, {"phrase": "my email", "type": "macro"})


def test_date_trigger_needs_no_payload(store):
    result = actions.save_trigger(store, {"phrase": "today", "type": "date"})
    assert result.triggers[0].type == "date"


def test_unsupported_type_is_refused_with_a_useful_message(store):
    """`transform` is D8 proper; refusing it must say what *is* supported."""
    with pytest.raises(ActionError, match="macro and date"):
        actions.save_trigger(store, {"phrase": "polish", "type": "transform", "payload": "x"})


def test_duplicate_phrase_is_refused(store):
    actions.save_trigger(store, {"phrase": "my email", "payload": "a@b.co"})
    with pytest.raises(ActionError, match="already uses that phrase"):
        actions.save_trigger(store, {"phrase": "My Email.", "payload": "other"})


def test_editing_keeps_its_own_phrase(store):
    saved = actions.save_trigger(store, {"phrase": "my email", "payload": "a@b.co"})
    trigger_id = saved.triggers[0].id
    updated = actions.save_trigger(
        store, {"id": trigger_id, "phrase": "my email", "payload": "changed"}
    )
    assert len(updated.triggers) == 1
    assert updated.triggers[0].payload == "changed"


def test_editing_preserves_list_position(store):
    actions.save_trigger(store, {"phrase": "first", "payload": "1"})
    second = actions.save_trigger(store, {"phrase": "second", "payload": "2"})
    actions.save_trigger(store, {"phrase": "third", "payload": "3"})

    target = second.triggers[1].id
    result = actions.save_trigger(store, {"id": target, "phrase": "second", "payload": "edited"})
    assert [t.phrase for t in result.triggers] == ["first", "second", "third"]


def test_phrase_length_is_capped(store):
    with pytest.raises(ActionError, match="too long"):
        actions.save_trigger(store, {"phrase": "x" * (MAX_PHRASE_CHARS + 1), "payload": "y"})


def test_a_phrase_of_only_punctuation_is_refused(store):
    with pytest.raises(ActionError, match="no words"):
        actions.save_trigger(store, {"phrase": "...", "payload": "y"})


def test_delete_is_idempotent(store):
    saved = actions.save_trigger(store, {"phrase": "my email", "payload": "a@b.co"})
    trigger_id = saved.triggers[0].id
    assert actions.delete_trigger(store, {"id": trigger_id}).triggers == ()
    # Deleting again must not error: the UI deletes optimistically.
    assert actions.delete_trigger(store, {"id": trigger_id}).triggers == ()


def test_delete_without_an_id_is_an_error(store):
    with pytest.raises(ActionError, match="which trigger"):
        actions.delete_trigger(store, {})


def test_trigger_view_reports_conflicts_by_phrase(store):
    store.update(
        triggers=(
            Trigger(id="a", phrase="my email", payload="1"),
            Trigger(id="b", phrase="my email address", payload="2"),
        )
    )
    view = actions.trigger_view(store.get())
    assert view[0]["conflicts"] == ["my email address"]
    assert view[1]["conflicts"] == ["my email"]


# -- app rules -------------------------------------------------------------


def test_saving_a_rule_twice_replaces_it(store):
    actions.save_app_rule(store, {"process": "Slack.exe", "style": "casual"})
    result = actions.save_app_rule(store, {"process": "slack.exe", "style": "formal"})
    assert len(result.app_rules) == 1
    assert result.app_rules[0].style == "formal"


def test_rule_with_unknown_style_is_refused(store):
    with pytest.raises(ActionError, match="unknown style"):
        actions.save_app_rule(store, {"process": "slack.exe", "style": "shouty"})


def test_delete_rule_is_case_insensitive(store):
    actions.save_app_rule(store, {"process": "slack.exe", "style": "casual"})
    assert actions.delete_app_rule(store, {"process": "SLACK.EXE"}).app_rules == ()


# -- dictionary ------------------------------------------------------------


def test_dictionary_add_and_remove(tmp_path):
    db = tmp_path / "dict.db"
    added = actions.dictionary_add(db, {"terms": ["Groq", "Kubernetes"]})
    assert added["added"] == 2
    assert set(added["terms"]) == {"Groq", "Kubernetes"}

    removed = actions.dictionary_remove(db, {"terms": ["Groq"]})
    assert removed["terms"] == ["Kubernetes"]


def test_dictionary_accepts_a_single_term(tmp_path):
    result = actions.dictionary_add(tmp_path / "d.db", {"term": "Groq"})
    assert result["terms"] == ["Groq"]


def test_dictionary_rejects_empty_input(tmp_path):
    for payload in ({}, {"terms": []}, {"terms": ["  ", ""]}):
        with pytest.raises(ActionError, match="no terms"):
            actions.dictionary_add(tmp_path / "d.db", payload)


def test_dictionary_caps_a_bulk_add(tmp_path):
    with pytest.raises(ActionError, match="too many"):
        actions.dictionary_add(tmp_path / "d.db", {"terms": [f"w{i}" for i in range(501)]})


# -- history ---------------------------------------------------------------


def test_history_delete_one(tmp_path):
    db = tmp_path / "history.db"
    with HistoryStore.open(db) as store:
        entry = store.add(kind=KIND_DICTATION, text="hello", raw_text="hello")
    assert actions.history_delete(db, {"id": entry.id})["deleted"] is True
    assert actions.history_delete(db, {"id": entry.id})["deleted"] is False


def test_history_delete_needs_an_id(tmp_path):
    with pytest.raises(ActionError, match="which entry"):
        actions.history_delete(tmp_path / "h.db", {})


def test_delete_all_requires_explicit_confirmation(tmp_path):
    """A stray or replayed POST must not wipe someone's entire history."""
    db = tmp_path / "history.db"
    with HistoryStore.open(db) as store:
        store.add(kind=KIND_DICTATION, text="hello", raw_text="hello")

    for payload in ({}, {"confirm": False}, {"confirm": "true"}):
        with pytest.raises(ActionError, match="explicit confirmation"):
            actions.history_delete_all(db, payload)

    assert actions.history_delete_all(db, {"confirm": True})["deleted"] == 1
    with HistoryStore.open(db) as store:
        assert store.count() == 0


# -- Diagnostics -------------------------------------------------------------


def test_recent_diagnostics_missing_file_reports_unavailable(tmp_path):
    result = actions.recent_diagnostics(tmp_path / "does-not-exist.log")
    assert result["available"] is False
    assert result["entries"] == []


def test_recent_diagnostics_none_path_reports_unavailable():
    result = actions.recent_diagnostics(None)
    assert result["available"] is False
    assert result["path"] == ""


def test_recent_diagnostics_filters_to_warnings_and_errors(tmp_path):
    log = tmp_path / "sway.log"
    log.write_text(
        "2026-08-25 22:42:01,278 24972 INFO    sway.engine [MainThread]: Sway ready.\n"
        "2026-08-25 23:07:19,282 37664 WARNING sway.engine [flow-transform]: "
        "transform output rejected by the markdown guardrail; leaving the selection alone\n"
        "2026-08-25 23:08:00,000 37664 ERROR   sway.groq [flow-net]: request failed\n",
        encoding="utf-8",
    )
    result = actions.recent_diagnostics(log)
    assert result["available"] is True
    levels = [e["level"] for e in result["entries"]]
    assert levels == ["ERROR", "WARNING"]  # newest first
    assert "markdown guardrail" in result["entries"][1]["message"]


def test_recent_diagnostics_folds_traceback_lines_into_their_entry(tmp_path):
    log = tmp_path / "sway.log"
    log.write_text(
        "2026-08-25 23:08:00,000 37664 ERROR   sway.groq [flow-net]: request failed\n"
        "Traceback (most recent call last):\n"
        '  File "engine.py", line 1, in <module>\n'
        "ValueError: boom\n",
        encoding="utf-8",
    )
    entries = actions.recent_diagnostics(log)["entries"]
    assert len(entries) == 1
    assert "Traceback" in entries[0]["message"]
    assert "ValueError: boom" in entries[0]["message"]


def test_recent_diagnostics_respects_limit(tmp_path):
    log = tmp_path / "sway.log"
    lines = [
        f"2026-08-25 23:00:{i:02d},000 1 WARNING sway.engine [T]: warning {i}\n"
        for i in range(10)
    ]
    log.write_text("".join(lines), encoding="utf-8")
    entries = actions.recent_diagnostics(log, limit=3)["entries"]
    assert len(entries) == 3
    assert entries[0]["message"] == "warning 9"  # newest first


# -- API key ---------------------------------------------------------------


def test_set_key_rejects_an_empty_value():
    for payload in ({}, {"key": ""}, {"key": "   "}, {"key": 42}):
        with pytest.raises(ActionError, match="no key given"):
            actions.set_key(payload)


# -- transform output expectations through the UI ---------------------------


def test_saving_a_transform_records_its_expectations(store):
    settings = actions.save_transform(
        store,
        {
            "name": "Bulletise",
            "instruction": "Rewrite as a list",
            "allows_markdown": True,
            "may_shorten": True,
        },
    )
    saved = settings.transforms[-1]
    assert saved.allows_markdown is True
    assert saved.may_shorten is True
    assert saved.may_lengthen is False


def test_resaving_a_builtin_does_not_drop_its_expectations(store):
    """An ordinary autosave posts the whole record back without the
    expectation keys. If that round trip lost them, re-saving Prompt
    Engineer's name would re-arm the markdown guardrail against its own
    structured output -- the original bug, restored by an unrelated edit."""
    prompt_engineer = next(t for t in store.get().transforms if t.id == "prompt_engineer")
    assert prompt_engineer.allows_markdown is True
    settings = actions.save_transform(
        store,
        {
            "id": "prompt_engineer",
            "name": prompt_engineer.name,
            "instruction": prompt_engineer.instruction,
        },
    )
    assert next(t for t in settings.transforms if t.id == "prompt_engineer").allows_markdown is True


def test_unticking_a_box_is_honoured(store):
    settings = actions.save_transform(
        store,
        {
            "id": "prompt_engineer",
            "name": "Prompt Engineer",
            "instruction": "Rewrite as a prompt",
            "allows_markdown": False,
        },
    )
    assert next(t for t in settings.transforms if t.id == "prompt_engineer").allows_markdown is False


def test_saving_a_transform_preserves_its_stored_example(store):
    """The editor never sends example_input/example_output back -- an
    ordinary save must not erase an example the engine recorded earlier."""
    prompt_engineer = next(t for t in store.get().transforms if t.id == "prompt_engineer")
    assert prompt_engineer.example_input
    settings = actions.save_transform(
        store,
        {
            "id": "prompt_engineer",
            "name": prompt_engineer.name,
            "instruction": "a changed instruction",
        },
    )
    saved = next(t for t in settings.transforms if t.id == "prompt_engineer")
    assert saved.example_input == prompt_engineer.example_input
    assert saved.example_output == prompt_engineer.example_output


def test_saving_a_transform_with_shortcuts(store):
    settings = actions.save_transform(
        store,
        {
            "name": "Bulletise",
            "instruction": "Rewrite as a list",
            "shortcuts": ["ctrl+alt+9", "mouse:middle"],
        },
    )
    saved = settings.transforms[-1]
    assert saved.shortcuts == ("ctrl+alt+9", "mouse:middle")


def test_saving_a_transform_rejects_an_unusable_shortcut(store):
    with pytest.raises(ActionError):
        actions.save_transform(
            store, {"name": "X", "instruction": "i", "shortcuts": ["just-a-letter"]}
        )


def test_saving_a_transform_does_not_reject_a_binding_that_merely_overlaps_dictation(store):
    """A transform shortcut sharing a modifier with dictation's bare Ctrl_r
    (Ctrl+Win, the user-reported bug) is not a conflict at all --
    sway.hotkey.BindingListener now resolves the ambiguity itself at
    dispatch time (prefix grace extended to bare holds), so both work
    correctly and independently. No `effective` plumbing is needed here any
    more since there is nothing left to check a candidate shortcut against."""
    settings = actions.save_transform(
        store, {"name": "Email", "instruction": "i", "shortcuts": ["ctrl+cmd"]}
    )
    assert settings.transforms[-1].shortcuts == ("ctrl+cmd",)


def test_transform_view_reports_no_conflict_for_a_binding_that_merely_overlaps_dictation(store):
    settings = actions.save_transform(
        store, {"name": "Email", "instruction": "i", "shortcuts": ["ctrl+cmd"]}
    )
    view = {row["id"]: row for row in actions.transform_view(settings)}
    saved = next(row for row in view.values() if row["name"] == "Email")
    assert saved["conflicts"] == []


def test_activation_view_reports_no_conflict_for_a_binding_that_merely_overlaps_a_transform(store):
    settings = actions.save_transform(
        store, {"name": "Email", "instruction": "i", "shortcuts": ["ctrl+cmd"]}
    )
    view = actions.activation_view(settings, effective={"hotkey": "ctrl_r"})
    assert view["dictation"]["conflicts"] == []


def test_the_transform_view_exposes_the_expectations(store):
    view = {row["id"]: row for row in actions.transform_view(store.get())}
    assert view["prompt_engineer"]["allows_markdown"] is True
    assert view["email"]["may_lengthen"] is True
    assert view["polish"]["allows_markdown"] is False


def test_the_transform_view_includes_a_precomputed_example_diff(store):
    view = {row["id"]: row for row in actions.transform_view(store.get())}
    polish = view["polish"]
    assert polish["example_diff"]
    assert any(part["op"] == "delete" for part in polish["example_diff"])
    custom = actions.save_transform(store, {"name": "X", "instruction": "i"})
    view = {row["id"]: row for row in actions.transform_view(custom)}
    assert view[custom.transforms[-1].id]["example_diff"] == []


def test_the_transform_view_reports_shortcuts_and_runnability(store):
    view = {row["id"]: row for row in actions.transform_view(store.get())}
    assert view["polish"]["shortcuts"] == ["ctrl+alt+5"]
    assert view["polish"]["shortcut_labels"] == ["Ctrl + Alt + 5"]
    assert view["polish"]["runnable"] is True
