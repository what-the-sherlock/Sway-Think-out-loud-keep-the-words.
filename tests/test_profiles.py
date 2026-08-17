"""Tests for the process-name -> cleanup-style profile matcher. Pure function,
no OS interaction, per CLAUDE.md.
"""

from __future__ import annotations

from flow.profiles import DEFAULT_STYLE, CleanupStyle, style_for_process


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
