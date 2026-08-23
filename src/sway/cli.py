"""Command-line entry point.

`demo`/`report`/`config` are Phase 0's measurement harness. `run` is the
actual app: hold the hotkey, speak, release, and cleaned-up text (Phase 2:
llama-3.1-8b-instant strips filler words and fixes grammar, in a style
chosen per focused app -- Phase 3) is pasted at the cursor, or hold the
command hotkey with text selected to apply a spoken instruction to it
(Phase 5). `devices` lists microphones for [audio].device. `dictionary`
manages the personal dictionary (Phase 3) that biases transcription toward
proper nouns and jargon. `history` lists/searches recorded dictations and
commands (Phase 5) from the terminal; the tray icon's "Show History" opens
the same data in a searchable window while `flow run` is active.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from sway import APP_NAME, __version__
from sway.config import Config, load_config
from sway.platform import get_platform
from sway.errors import AudioError, SwayError, LatencyStoreError
from sway.latency.report import format_table, summarize
from sway.latency.store import LatencyStore
from sway.latency.tracker import LatencyTracker
from sway.logging_setup import configure_logging
from sway.pipeline.stub import STUB_STAGES, StubPipeline

logger = logging.getLogger(__name__)

_DEFAULT_DEMO_RUNS = 20
_DEFAULT_REPORT_LIMIT = 100
_DEFAULT_HISTORY_LIMIT = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sway",
        description=f"{APP_NAME} - local voice dictation with LLM cleanup.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, metavar="PATH", help="path to a config.toml")
    parser.add_argument("--data-dir", type=Path, metavar="PATH", help="override the data directory")
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        help="override [logging].level (DEBUG, INFO, WARNING, ERROR)",
    )

    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser(
        "demo",
        help="run the stub pipeline and print a latency table",
        description=(
            "Runs fake dictations through the real latency tracker. Phase 0's "
            "proof that measurement works before any feature exists."
        ),
    )
    demo.add_argument(
        "-n", "--runs", type=int, default=_DEFAULT_DEMO_RUNS, help="number of fake dictations"
    )
    demo.add_argument("--seed", type=int, default=1234, help="RNG seed for reproducible timings")
    demo.add_argument(
        "--fail-rate",
        type=float,
        default=0.0,
        metavar="P",
        help="probability [0-1] that a run fails, to exercise the error path",
    )
    demo.set_defaults(handler=_cmd_demo)

    report = sub.add_parser(
        "report",
        help="print the latency table from recorded history",
    )
    group = report.add_mutually_exclusive_group()
    group.add_argument(
        "-n",
        "--last",
        type=int,
        default=_DEFAULT_REPORT_LIMIT,
        metavar="N",
        help=f"summarise the last N runs (default {_DEFAULT_REPORT_LIMIT})",
    )
    group.add_argument("--all", action="store_true", help="summarise every recorded run")
    report.add_argument(
        "--since-hours", type=float, metavar="H", help="only include runs from the last H hours"
    )
    report.set_defaults(handler=_cmd_report)

    show = sub.add_parser("config", help="print the resolved configuration")
    show.set_defaults(handler=_cmd_config)

    startup = sub.add_parser(
        "startup",
        help="show or change whether Sway starts automatically at login",
    )
    startup_group = startup.add_mutually_exclusive_group()
    startup_group.add_argument(
        "--enable", action="store_true", help="start Sway when you log in"
    )
    startup_group.add_argument(
        "--disable", action="store_true", help="stop starting Sway at login"
    )
    startup.set_defaults(handler=_cmd_startup)

    run = sub.add_parser(
        "run",
        help="run Sway: hold the hotkey, speak, release to paste the transcript",
    )
    run.set_defaults(handler=_cmd_run)

    devices = sub.add_parser("devices", help="list available microphones for [audio].device")
    devices.set_defaults(handler=_cmd_devices)

    dictionary = sub.add_parser(
        "dictionary",
        help="manage the personal dictionary used to bias transcription",
        description=(
            "Terms added here are injected into Whisper's prompt to bias "
            "transcription toward proper nouns and jargon it would otherwise "
            "mishear (Phase 3)."
        ),
    )
    dict_sub = dictionary.add_subparsers(dest="dict_command", required=True)

    dict_add = dict_sub.add_parser("add", help="add one or more terms")
    dict_add.add_argument("terms", nargs="+", metavar="TERM")
    dict_add.set_defaults(handler=_cmd_dictionary_add)

    dict_remove = dict_sub.add_parser("remove", help="remove one or more terms")
    dict_remove.add_argument("terms", nargs="+", metavar="TERM")
    dict_remove.set_defaults(handler=_cmd_dictionary_remove)

    dict_list = dict_sub.add_parser("list", help="list every term")
    dict_list.set_defaults(handler=_cmd_dictionary_list)

    dashboard = sub.add_parser(
        "dashboard",
        help="open the Sway dashboard in a browser",
        description=(
            "Serves the dashboard on loopback and opens it. Standalone: it "
            "reports the history and latency already recorded, but cannot "
            "pause or resume dictation, because no engine is attached. To "
            "control a running engine, use the tray icon's 'Open Dashboard' "
            "item instead."
        ),
    )
    dashboard.add_argument(
        "--port", type=int, default=0, metavar="PORT", help="bind a fixed port (default: any free port)"
    )
    dashboard.add_argument(
        "--no-browser", action="store_true", help="print the URL instead of opening a browser"
    )
    dashboard.set_defaults(handler=_cmd_dashboard)

    history = sub.add_parser(
        "history",
        help="search or list past dictations and voice commands",
        description=(
            "Every successful dictation and voice command is recorded "
            "(Phase 5) so it can be found again -- see also the searchable "
            "history window on the tray icon's 'Show History' menu item."
        ),
    )
    history_sub = history.add_subparsers(dest="history_command", required=True)

    history_list = history_sub.add_parser("list", help="list history entries, newest first")
    history_list.add_argument(
        "-n", "--last", type=int, default=_DEFAULT_HISTORY_LIMIT, metavar="N", help="show at most N entries"
    )
    history_list.add_argument("-q", "--search", metavar="QUERY", help="only entries containing QUERY")
    history_list.add_argument(
        "--kind", choices=["dictation", "command"], help="only entries of this kind"
    )
    history_list.set_defaults(handler=_cmd_history_list)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "handler", None) is None:
        parser.print_help()
        return 0

    try:
        config = _load(args)
        configure_logging(config, force_level=args.log_level)
        # After logging is configured (so the migration is visible in the log
        # file) but before any handler touches a database. Never raises.
        _migrate_from_legacy(config)
        return args.handler(args, config)
    except SwayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _load(args: argparse.Namespace) -> Config:
    env = dict(os.environ)
    if args.data_dir is not None:
        env["SWAY_DATA_DIR"] = str(args.data_dir)
    return load_config(config_file=args.config, env=env)


def _migrate_from_legacy(config: Config) -> None:
    """Carry a pre-rename install forward. Best-effort by contract.

    Imported lazily and wrapped: a migration problem must never be the reason
    the app will not start, since the app works perfectly well with a fresh
    data directory -- the user would just have lost their history, which the
    log will say.
    """
    try:
        from sway.migrate import migrate

        migrate(data_dir=config.data_dir)
    except Exception:
        logger.warning("migration from the previous name failed", exc_info=True)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _cmd_demo(args: argparse.Namespace, config: Config) -> int:
    if args.runs < 1:
        print("error: --runs must be at least 1", file=sys.stderr)
        return 2
    if not 0.0 <= args.fail_rate <= 1.0:
        print("error: --fail-rate must be between 0 and 1", file=sys.stderr)
        return 2

    expected_s = sum((s.min_ms + s.max_ms) / 2 for s in STUB_STAGES) * args.runs / 1000.0
    print(
        f"Running {args.runs} stub dictation(s) (~{expected_s:.0f}s), "
        f"recording to {config.latency_db_path}",
        file=sys.stderr,
    )

    started = time.perf_counter()
    with LatencyTracker.from_config(config) as tracker:
        if not tracker.enabled:
            print("error: latency tracking is disabled in config", file=sys.stderr)
            return 2
        pipeline = StubPipeline(tracker, seed=args.seed, fail_rate=args.fail_rate)
        succeeded, failed = pipeline.run(args.runs)
        if not tracker.flush(timeout=10.0):
            print("warning: latency records may not have been fully written", file=sys.stderr)

        try:
            with LatencyStore.open(config.latency_db_path) as store:
                runs = store.fetch_runs(limit=args.runs)
        except LatencyStoreError as exc:
            # The tracker already logged why and degraded; the pipeline itself
            # ran fine. Only the table — this command's whole point — is lost.
            print(f"error: pipeline ran, but timings could not be read back: {exc}", file=sys.stderr)
            return 1

    elapsed = time.perf_counter() - started
    print(
        f"Completed {succeeded} succeeded / {failed} failed in {elapsed:.1f}s\n",
        file=sys.stderr,
    )
    print(format_table(summarize(runs), title="Stub pipeline latency"))
    return 0


def _cmd_report(args: argparse.Namespace, config: Config) -> int:
    if not config.latency_db_path.exists():
        print(
            f"error: no latency database at {config.latency_db_path}; run 'flow demo' first",
            file=sys.stderr,
        )
        return 1

    since = None
    if args.since_hours is not None:
        if args.since_hours <= 0:
            print("error: --since-hours must be positive", file=sys.stderr)
            return 2
        since = time.time() - args.since_hours * 3600.0

    limit = None if args.all else max(1, args.last)
    with LatencyStore.open(config.latency_db_path) as store:
        runs = store.fetch_runs(limit=limit, since=since)
        total = store.count_runs()

    window = "all runs" if args.all else f"newest {limit}"
    if since is not None:
        window += f" within {args.since_hours:g}h"
    print(format_table(summarize(runs), title=f"Stage latency ({window}; {total} recorded overall)"))
    return 0


def _cmd_config(args: argparse.Namespace, config: Config) -> int:
    del args
    for key, value in config.redacted_dict().items():
        if isinstance(value, dict):
            print(f"{key}:")
            for sub_key, sub_value in value.items():
                print(f"  {sub_key} = {sub_value}")
        else:
            print(f"{key} = {value}")
    return 0


def startup_command() -> str | None:
    """The command line Windows should run at login, or ``None`` if unknown.

    In a frozen build this is ``Sway.exe`` itself. From source there is no
    single sensible answer -- the venv's python, the module, and the working
    directory all matter -- and registering a half-guessed command would
    leave a login entry that fails silently every morning. Better to decline.
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return None


def _cmd_startup(args: argparse.Namespace, config: Config) -> int:
    del config
    platform = get_platform()

    if not args.enable and not args.disable:
        state = "enabled" if platform.launches_at_login(APP_NAME) else "disabled"
        print(f"start at login: {state}")
        return 0

    if args.disable:
        ok = platform.set_launch_at_login(APP_NAME, None)
        print("start at login: disabled" if ok else "could not change the setting")
        return 0 if ok else 1

    command = startup_command()
    if command is None:
        print(
            "error: only the packaged build can register itself to start at login.\n"
            "Running from source, the command to register is ambiguous (which\n"
            "interpreter, which working directory), so Sway will not guess.",
            file=sys.stderr,
        )
        return 2
    ok = platform.set_launch_at_login(APP_NAME, command)
    print(f"start at login: enabled ({command})" if ok else "could not change the setting")
    return 0 if ok else 1


def _cmd_run(args: argparse.Namespace, config: Config) -> int:
    del args
    # Imported lazily so `flow demo`/`report`/`config` never pay for
    # importing sounddevice/pynput/httpx just to answer a Phase 0 question.
    from sway.engine import Engine

    if not config.groq_api_key:
        # Not fatal any more. Onboarding lives inside the app window, so the
        # app has to be running for the user to be able to give it a key at
        # all. A console run still says so, because a terminal user is not
        # going to see the window's onboarding by accident.
        print(
            "note: no Groq API key yet — Sway will ask for one in its window.",
            file=sys.stderr,
        )

    tracker = LatencyTracker.from_config(config)
    engine = Engine(config, tracker)
    try:
        engine.start()
        from sway.hotkey import prettify_hotkey

        print(
            f"Sway is running. Hold {prettify_hotkey(config.hotkey.key)} to dictate. "
            "Ctrl+C to quit.",
            file=sys.stderr,
        )
        engine.run_ui()
    except KeyboardInterrupt:
        # Ctrl+C from a console run is a normal quit, not a crash. The
        # `finally` below still performs the full shutdown.
        logger.info("interrupted; shutting down")
    except BaseException:
        # Log before re-raising. flow.gui shows the traceback in a dialog,
        # but a dialog is transient and the windowed build has no console --
        # without this, the only durable record of a crash would be whatever
        # the user managed to read before clicking OK. Logged here rather
        # than in flow.gui so `python -m flow run` gets it too.
        logger.exception("Sway stopped with an unhandled error")
        raise
    finally:
        # Runs on every path including a crash, which is what keeps a failed
        # run from leaving the hotkey hooked and the mic held (D1).
        engine.stop()
        tracker.close()
    return 0


def _cmd_dashboard(args: argparse.Namespace, config: Config) -> int:
    """Serve the dashboard until interrupted.

    Blocks on purpose: the server runs on a daemon thread, so returning
    immediately would tear it down before the browser finished loading.
    """
    import webbrowser

    from sway.dashboard.server import DashboardServer

    server = DashboardServer(
        history_db=config.history_db_path,
        latency_db=config.latency_db_path,
        typing_wpm=config.analytics.typing_wpm,
        port=args.port,
    )
    url = server.start()
    print(f"Sway dashboard: {url}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _cmd_devices(args: argparse.Namespace, config: Config) -> int:
    del args, config
    # Imported lazily; see _cmd_run.
    from sway.audio.capture import list_input_devices

    try:
        devices = list_input_devices()
    except AudioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("no input devices found", file=sys.stderr)
        return 1
    for line in devices:
        print(line)
    return 0


def _cmd_dictionary_add(args: argparse.Namespace, config: Config) -> int:
    from sway.dictionary import DictionaryStore

    with DictionaryStore.open(config.dictionary_db_path) as store:
        added = store.add_many(args.terms)
        total = len(store.list_terms())
    print(f"added {added} new term(s); {total} total", file=sys.stderr)
    return 0


def _cmd_dictionary_remove(args: argparse.Namespace, config: Config) -> int:
    from sway.dictionary import DictionaryStore

    with DictionaryStore.open(config.dictionary_db_path) as store:
        removed = store.remove_many(args.terms)
        total = len(store.list_terms())
    print(f"removed {removed} term(s); {total} remaining", file=sys.stderr)
    return 0


def _cmd_dictionary_list(args: argparse.Namespace, config: Config) -> int:
    del args
    from sway.dictionary import DictionaryStore

    with DictionaryStore.open(config.dictionary_db_path) as store:
        terms = store.list_terms()

    if not terms:
        print("the dictionary is empty; add terms with 'flow dictionary add'", file=sys.stderr)
        return 0
    for term in terms:
        print(term)
    return 0


def _cmd_history_list(args: argparse.Namespace, config: Config) -> int:
    if not config.history_db_path.exists():
        print(
            f"error: no history database at {config.history_db_path}; "
            "run 'flow run' and dictate something first",
            file=sys.stderr,
        )
        return 1
    if args.last < 1:
        print("error: --last must be at least 1", file=sys.stderr)
        return 2

    from sway.history import HistoryStore

    with HistoryStore.open(config.history_db_path) as store:
        entries = store.search(args.search or "", kind=args.kind, limit=args.last)

    if not entries:
        print("no matching history entries", file=sys.stderr)
        return 0

    for entry in entries:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.created_at))
        preview = _one_line(entry.text, limit=100)
        if entry.kind == "command" and entry.instruction:
            print(f"{when}  command     [{_one_line(entry.instruction, limit=40)}] {preview}")
        else:
            print(f"{when}  dictation   {preview}")
    return 0


def _one_line(text: str, *, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
