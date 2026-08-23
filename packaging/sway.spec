# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Sway (Phase 5).

Produces `dist/flow/` -- a self-contained Windows build that needs no Python
install on the target machine. Build with:

    pyinstaller packaging/flow.spec

Two executables share one collected directory, because the app has two
genuinely different audiences:

* `Sway.exe` -- windowed (no console). What you double-click, and what you
  hand to someone else. Bare launch means `run`; see flow.gui for why that,
  and the message dialogs, exist.
* `flow-cli.exe` -- console. The terminal tools (`config`, `report`,
  `history`, `dictionary`, `devices`, `demo`), which need a real stdout.

A windowed build cannot host the CLI (its output would vanish) and a console
build cannot be double-clicked cleanly (it flashes a console and prints
help), so both exist rather than compromising on one.

`pyinstaller-hooks-contrib` (pulled in transitively by the `build` extra --
see pyproject.toml) already ships hooks for sounddevice, soundfile, pynput,
and pystray that collect their native libraries (PortAudio, libsndfile) and
dynamically-loaded per-OS backend modules automatically. This spec only adds
what's specific to Sway itself: its own package and assets, the example
config/env files a fresh install needs, a defensive hidden-import for
webrtcvad-wheels' compiled extension, the entry-point-resolved keyring
backend, the optional pywebview runtime, and (via hookspath) a local
override for hooks-contrib's stock webrtcvad hook, which is broken for the
webrtcvad-wheels fork this project depends on -- see hooks/hook-webrtcvad.py.

The build deliberately carries **no API key**. scripts/build_app.py used to
copy the developer's .env next to the exe; it no longer does, because dist/
is what gets zipped and handed over. Keys go to Windows Credential Manager
through onboarding instead -- see flow.credentials.

`--onedir` (COLLECT below), not `--onefile`: a background dictation app
should start in well under a second, and onefile's self-extraction to a temp
directory on every launch works against exactly that. `dist/flow/` is the
whole shippable unit -- zip it, or hand it to an installer tool, as-is.
"""

import sys
from pathlib import Path

# Unlike Analysis/PYZ/EXE/COLLECT, the version-resource types are not
# injected into a spec file's namespace by PyInstaller and must be imported.
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

# PyInstaller execs this file rather than importing it, injecting SPECPATH
# (the directory containing this .spec) into its namespace -- there is no
# __file__ here.
PROJECT_ROOT = Path(SPECPATH).resolve().parent
SRC = PROJECT_ROOT / "src"

# Read the version from the package itself so the exe's file properties can
# never drift from sway.__version__.
sys.path.insert(0, str(SRC))
from sway import APP_NAME, __version__  # noqa: E402

ICON = PROJECT_ROOT / "packaging" / "sway.ico"
if not ICON.is_file():
    raise SystemExit(
        f"missing {ICON}\nGenerate it first:  python scripts/build_app_icon.py"
    )

datas = [
    (str(PROJECT_ROOT / "config.example.toml"), "."),
    # flow.branding loads this at runtime via a path relative to its own
    # module file (Path(__file__).parent / "assets"), which PyInstaller's
    # static import analysis cannot see -- unlike config.example.toml above,
    # this has to land in the *package's* collected directory, not the
    # bundle root, or the overlay pill/tray icon/window icons have nothing
    # to render. See scripts/build_logo_asset.py for how it is generated.
    (str(SRC / "sway" / "assets" / "logo_mask.png"), "sway/assets"),
]

# The dashboard's static files, for the same reason as logo_mask.png above:
# flow.dashboard.server resolves them relative to its own module file
# (Path(__file__).parent / "static"), which no static import analysis can
# see. Without these the packaged app starts and dictates normally but the
# dashboard serves 404 for every asset. Collected as a directory so adding a
# file to static/ never needs this spec edited again.
for _asset in sorted((SRC / "sway" / "dashboard" / "static").iterdir()):
    if _asset.is_file():
        datas.append((str(_asset), "sway/dashboard/static"))
env_example = PROJECT_ROOT / ".env.example"
if env_example.is_file():
    datas.append((str(env_example), "."))

# webrtcvad-wheels' compiled extension is a plain top-level import
# (`import _webrtcvad` inside webrtcvad.py) that PyInstaller's static
# analysis normally traces on its own; listed explicitly as a defensive
# belt-and-braces measure since, unlike sounddevice/soundfile/pynput/
# pystray, it has no hook of its own in hooks-contrib.
hiddenimports = ["_webrtcvad"]

# keyring resolves its backend through entry points at runtime, so static
# analysis sees no import of the Windows vault backend at all -- the frozen
# app would fall back to the null backend and report "no credential store",
# stranding the user on the API-key step of onboarding.
hiddenimports += [
    "keyring.backends.Windows",
    "keyring.backends.fail",
    "keyring.backends.null",
]

# pywebview is optional (see pyproject's `window` extra). When it is
# installed, collect it whole: it loads its per-platform backend by name at
# runtime, and on Windows that pulls in pythonnet/clr_loader plus the
# WebView2 interop assemblies, none of which are reachable by static
# analysis. When it is absent the build simply omits it and flow.window
# falls back to the browser, so this never breaks a build machine that has
# not installed the extra.
try:
    from PyInstaller.utils.hooks import collect_all

    _webview_datas, _webview_binaries, _webview_hidden = collect_all("webview")
except Exception:
    _webview_datas, _webview_binaries, _webview_hidden = [], [], []
else:
    datas += _webview_datas
    hiddenimports += _webview_hidden + [
        "webview.platforms.edgechromium",
        "clr_loader",
        "pythonnet",
    ]

# Shadows pyinstaller-hooks-contrib's hook-webrtcvad.py, which is broken for
# the webrtcvad-wheels fork this project uses -- see hooks/hook-webrtcvad.py.
hookspath = [str(PROJECT_ROOT / "packaging" / "hooks")]


def _analysis(entry_script):
    return Analysis(
        [str(PROJECT_ROOT / "packaging" / entry_script)],
        pathex=[str(SRC)],
        binaries=_webview_binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=hookspath,
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
    )


# Windows file-properties metadata. Unsigned builds still trip SmartScreen
# (see README), but an exe with no version resource at all looks even less
# trustworthy, and "which build is this?" is otherwise unanswerable once a
# copy has been passed around.
_version_tuple = tuple(int(part) for part in __version__.split(".")[:3]) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_version_tuple, prodvers=_version_tuple),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",  # US English, Unicode
                    [
                        StringStruct("FileDescription", "Local voice dictation with LLM cleanup"),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", APP_NAME),
                        StringStruct("OriginalFilename", "Sway.exe"),
                        StringStruct("ProductName", APP_NAME),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

gui_analysis = _analysis("entry_gui.py")
cli_analysis = _analysis("entry_cli.py")

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    # Capitalised, and distinct from sway-cli.exe by more than case: Windows
    # filenames are case-insensitive, so this and the console build cannot
    # differ only in capitalisation.
    name="Sway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON),
    version=version_info,
)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="sway-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON),
    version=version_info,
)

# One COLLECT for both executables: they share an almost identical
# dependency set (Python runtime, PortAudio, libsndfile, ...), so collecting
# them together deduplicates that instead of shipping it twice.
coll = COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.zipfiles,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.zipfiles,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="sway",
)
