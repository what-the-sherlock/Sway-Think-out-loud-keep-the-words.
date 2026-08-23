"""Phase 3: per-app cleanup profiles, chosen from the focused window's
process name.

Detection happens once per dictation via
:meth:`~flow.platform.PlatformAdapter.focused_process_name`; turning that
name into a profile is a pure function so it is fully unit-testable without
touching the OS or a real window.
"""

from __future__ import annotations

from enum import StrEnum


class CleanupStyle(StrEnum):
    """How the cleanup pass should treat text bound for this app."""

    #: Chat apps: a light touch that keeps the conversational tone.
    CASUAL = "casual"
    #: Mail clients: a more polished, professional rewrite.
    FORMAL = "formal"
    #: Terminals and code editors: no cleanup call at all. Commands and code
    #: must reach the cursor exactly as transcribed, not paraphrased.
    VERBATIM = "verbatim"


#: Default profile for any process not recognised below.
DEFAULT_STYLE = CleanupStyle.CASUAL

#: Executable basenames (no path, lower-cased, as Windows reports them) mapped
#: to the cleanup style their content calls for. Extend freely -- an
#: unrecognised process simply falls back to DEFAULT_STYLE.
_CHAT_APPS = frozenset(
    {
        "slack.exe",
        "discord.exe",
        "teams.exe",
        "ms-teams.exe",
        "telegram.exe",
        "whatsapp.exe",
        "signal.exe",
        "messenger.exe",
        "skype.exe",
        "element.exe",
    }
)

_MAIL_CLIENTS = frozenset(
    {
        "outlook.exe",
        "thunderbird.exe",
        "mailbird.exe",
        "mailspring.exe",
    }
)

_TERMINALS = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "windowsterminal.exe",
        "conhost.exe",
        "conemu64.exe",
        "conemu.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
        "hyper.exe",
        "mintty.exe",
        "git-bash.exe",
    }
)

_CODE_EDITORS = frozenset(
    {
        "code.exe",
        "cursor.exe",
        "devenv.exe",
        "pycharm64.exe",
        "pycharm.exe",
        "idea64.exe",
        "webstorm64.exe",
        "clion64.exe",
        "rider64.exe",
        "goland64.exe",
        "sublime_text.exe",
        "notepad++.exe",
        "vim.exe",
        "gvim.exe",
        "nvim-qt.exe",
        "atom.exe",
    }
)

_PROFILE_BY_PROCESS: dict[str, CleanupStyle] = {
    **dict.fromkeys(_CHAT_APPS, CleanupStyle.CASUAL),
    **dict.fromkeys(_MAIL_CLIENTS, CleanupStyle.FORMAL),
    **dict.fromkeys(_TERMINALS | _CODE_EDITORS, CleanupStyle.VERBATIM),
}


def style_for_process(process_name: str | None) -> CleanupStyle:
    """Pick a cleanup style from a focused-window process name.

    Unknown or undetectable (``None``) processes fall back to
    :data:`DEFAULT_STYLE` -- the general-purpose cleanup Phase 2 shipped
    with. Matching is case-insensitive and tolerant of surrounding
    whitespace, since the caller may pass whatever the platform layer
    returned verbatim.
    """
    if not process_name:
        return DEFAULT_STYLE
    return _PROFILE_BY_PROCESS.get(process_name.strip().lower(), DEFAULT_STYLE)


__all__ = ["DEFAULT_STYLE", "CleanupStyle", "style_for_process"]
