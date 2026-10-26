"""Window size/position restore (DESIGN.md §5.6). Pure file I/O, no GUI."""

from __future__ import annotations

import json

from sway.window import MIN_HEIGHT, MIN_WIDTH, load_window_state, save_window_state


def test_round_trip(tmp_path):
    path = tmp_path / "window.json"
    save_window_state(path, {"width": 1300, "height": 900, "x": 40, "y": 60, "maximized": True})
    assert load_window_state(path) == {"width": 1300, "height": 900, "x": 40, "y": 60, "maximized": True}


def test_missing_or_corrupt_file_is_no_state(tmp_path):
    assert load_window_state(None) == {}
    assert load_window_state(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_window_state(bad) == {}
    bad.write_text("[1, 2]", encoding="utf-8")
    assert load_window_state(bad) == {}


def test_size_is_clamped_to_the_minimum(tmp_path):
    path = tmp_path / "window.json"
    path.write_text(json.dumps({"width": 300, "height": 200}), encoding="utf-8")
    assert load_window_state(path) == {"width": MIN_WIDTH, "height": MIN_HEIGHT}


def test_an_implausible_position_is_dropped(tmp_path):
    """A monitor that has been unplugged must not strand the window."""
    path = tmp_path / "window.json"
    path.write_text(json.dumps({"width": 1200, "height": 800, "x": 99_999, "y": 10}), encoding="utf-8")
    assert load_window_state(path) == {"width": 1200, "height": 800}


def test_wrong_types_are_ignored(tmp_path):
    path = tmp_path / "window.json"
    path.write_text(json.dumps({"width": "big", "x": 1.5, "y": 2, "maximized": "yes"}), encoding="utf-8")
    assert load_window_state(path) == {}


def test_save_keeps_only_known_keys(tmp_path):
    path = tmp_path / "window.json"
    save_window_state(path, {"width": 1000, "height": 700, "token": "secret"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"width": 1000, "height": 700}


def test_save_to_an_unwritable_place_does_not_raise(tmp_path):
    save_window_state(tmp_path / "no" / "such" / "dir" / "window.json", {"width": 1000, "height": 700})
    save_window_state(None, {"width": 1000})
