"""Tests for the process-name -> cleanup-style profile matcher. Pure function,
no OS interaction, per CLAUDE.md.
"""

from __future__ import annotations

from sway.profiles import DEFAULT_STYLE, CleanupStyle, style_for_process


def test_unknown_process_falls_back_to_default():
    assert style_for_process("some_random_app.exe") == DEFAULT_STYLE
    assert DEFAULT_STYLE == CleanupStyle.CASUAL


def test_none_process_falls_back_to_default():
    assert style_for_process(None) == DEFAULT_STYLE


def test_chat_apps_are_casual():
    assert style_for_process("slack.exe") == CleanupStyle.CASUAL
    assert style_for_process("discord.exe") == CleanupStyle.CASUAL
    assert style_for_process("teams.exe") == CleanupStyle.CASUAL


def test_mail_clients_are_formal():
    assert style_for_process("outlook.exe") == CleanupStyle.FORMAL
    assert style_for_process("thunderbird.exe") == CleanupStyle.FORMAL


def test_terminals_are_verbatim():
    assert style_for_process("cmd.exe") == CleanupStyle.VERBATIM
    assert style_for_process("powershell.exe") == CleanupStyle.VERBATIM
    assert style_for_process("windowsterminal.exe") == CleanupStyle.VERBATIM


def test_code_editors_are_verbatim():
    assert style_for_process("code.exe") == CleanupStyle.VERBATIM
    assert style_for_process("pycharm64.exe") == CleanupStyle.VERBATIM
    assert style_for_process("vim.exe") == CleanupStyle.VERBATIM


def test_matching_is_case_insensitive():
    assert style_for_process("SLACK.EXE") == CleanupStyle.CASUAL
    assert style_for_process("Code.exe") == CleanupStyle.VERBATIM


def test_matching_tolerates_surrounding_whitespace():
    assert style_for_process("  slack.exe  ") == CleanupStyle.CASUAL


def test_empty_string_falls_back_to_default():
    assert style_for_process("") == DEFAULT_STYLE


# -- app categories (shown on the Style page and the Insights usage chart) --

from sway.profiles import AppCategory, category_for_process, default_groups  # noqa: E402


def test_categories_follow_the_style_lists():
    assert category_for_process("slack.exe") == AppCategory.CHAT
    assert category_for_process("OUTLOOK.EXE ") == AppCategory.MAIL
    assert category_for_process("pwsh.exe") == AppCategory.TERMINAL
    assert category_for_process("code.exe") == AppCategory.CODE


def test_unknown_or_missing_process_is_other():
    assert category_for_process("notepad.exe") == AppCategory.OTHER
    assert category_for_process(None) == AppCategory.OTHER
    assert category_for_process("") == AppCategory.OTHER


def test_default_groups_describe_what_the_engine_applies():
    """The Style page reads these; each group's style must be the one
    style_for_process actually returns for every app listed in it."""
    groups = default_groups()
    assert [g["category"] for g in groups] == ["chat", "mail", "terminal", "code", "other"]
    for group in groups:
        for app in group["apps"]:
            assert style_for_process(app).value == group["style"]
    other = groups[-1]
    assert other["apps"] == [] and other["style"] == DEFAULT_STYLE.value
