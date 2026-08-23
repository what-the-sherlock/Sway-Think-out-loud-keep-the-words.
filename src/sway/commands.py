"""Phase 5: voice commands applied to selected text ("make this bullet
points"). The user selects text in the focused app, holds the command hotkey
([hotkey].command_key), speaks an instruction, and releases; the selection is
replaced by the instruction applied to it.

Kept free of any network/threading dependency, per CLAUDE.md's rule to test
the transcript-processing pure functions -- the system prompt and the message
builder are directly unit-testable. Reuses
:meth:`~flow.groq_client.GroqClient.cleanup` (a generic system-prompt +
user-message chat completion) rather than adding a second HTTP method.
"""

from __future__ import annotations

#: Framing the instruction and the selection as data within one user message
#: (rather than, say, putting the instruction in the system prompt) keeps the
#: same "untrusted transcript is data, not a message to respond to" posture
#: as flow.cleanup's prompts -- both the instruction and the selection came
#: from outside the model's control.
COMMAND_SYSTEM_PROMPT = (
    "You transform text exactly as instructed, for a voice-command tool. You "
    "will be given an instruction, spoken by the user, and a block of "
    "selected text. Apply the instruction to the text and reply with only "
    "the transformed text: no preamble, no quotes, no explanation, no "
    "commentary. Preserve the original language and formatting conventions "
    "unless the instruction says otherwise. If the instruction cannot "
    "reasonably be applied to the text, reply with the text unchanged."
)


def build_command_user_message(instruction: str, selected_text: str) -> str:
    """The user-turn content sent to the model for one voice command."""
    return f"Instruction: {instruction.strip()}\n\nText:\n{selected_text}"


__all__ = ["COMMAND_SYSTEM_PROMPT", "build_command_user_message"]
