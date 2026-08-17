"""Tests for the voice-command prompt/message building (Phase 5)."""

from __future__ import annotations

from flow.commands import COMMAND_SYSTEM_PROMPT, build_command_user_message


def test_build_command_user_message_includes_instruction_and_text():
    result = build_command_user_message("make this bullet points", "milk\neggs\nbread")
    assert "make this bullet points" in result
    assert "milk\neggs\nbread" in result


def test_build_command_user_message_strips_instruction_whitespace():
    result = build_command_user_message("  fix grammar  ", "some text")
    assert result.startswith("Instruction: fix grammar\n")


def test_build_command_user_message_preserves_selected_text_verbatim():
    # Leading/trailing whitespace in the *selection* is left alone -- only
    # the spoken instruction is trimmed.
    selected = "  keep this exactly  "
    result = build_command_user_message("uppercase it", selected)
    assert selected in result


def test_command_system_prompt_instructs_output_only():
    # Regression guard for the "no preamble, no commentary" contract -- the
    # exact wording can change, but the prompt must keep insisting on it.
    assert "only" in COMMAND_SYSTEM_PROMPT.lower()
    assert "no preamble" in COMMAND_SYSTEM_PROMPT.lower()
