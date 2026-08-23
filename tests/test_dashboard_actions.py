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
    store.update(hotkey="ctrl_l", launch_at_login=True)
    actions.update_settings(store, {"launch_at_login": False})
    # An absent key means "leave it alone", so two controls saved
    # concurrently cannot clobber each other.
    assert store.get().hotkey == "ctrl_l"
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


# -- API key ---------------------------------------------------------------


def test_set_key_rejects_an_empty_value():
    for payload in ({}, {"key": ""}, {"key": "   "}, {"key": 42}):
        with pytest.raises(ActionError, match="no key given"):
            actions.set_key(payload)
