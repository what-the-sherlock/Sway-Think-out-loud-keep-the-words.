"""Phase 5's searchable history window: a live-filtered list of past
dictations and voice commands, backed by :class:`~flow.history.HistoryStore`.

Tkinter (stdlib), matching :mod:`flow.overlay` -- no new UI dependency for a
window this small. Must only ever be built via
:meth:`~flow.overlay.Overlay.run_on_ui_thread`, since it's plain Tk widgets
touched from whatever thread asked to open it (the tray icon's thread, most
often); this module itself is therefore untested wiring, like
:mod:`flow.overlay`, per CLAUDE.md.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from tkinter import ttk

from flow.history import KIND_COMMAND, KIND_DICTATION, HistoryEntry, HistoryStore

logger = logging.getLogger(__name__)

_WIDTH, _HEIGHT = 760, 480
_PREVIEW_CHARS = 90


class HistoryWindow:
    """Owns one Toplevel. :meth:`show` builds it once and raises it thereafter."""

    def __init__(self, parent: tk.Misc, store: HistoryStore) -> None:
        self._parent = parent
        self._store = store
        self._toplevel: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None
        self._detail: tk.Text | None = None
        self._search_var: tk.StringVar | None = None
        self._kind_var: tk.StringVar | None = None
        self._entries_by_row: dict[str, HistoryEntry] = {}

    def show(self) -> None:
        """Build the window on first call; on later calls just raise it."""
        if self._toplevel is not None and self._toplevel.winfo_exists():
            self._toplevel.deiconify()
            self._toplevel.lift()
            self._toplevel.focus_force()
            self._refresh()
            return
        self._build()

    def _build(self) -> None:
        top = tk.Toplevel(self._parent)
        self._toplevel = top
        top.title("Flow History")
        top.geometry(f"{_WIDTH}x{_HEIGHT}")
        top.minsize(480, 320)

        search_var = tk.StringVar()
        kind_var = tk.StringVar(value="all")
        self._search_var = search_var
        self._kind_var = kind_var

        bar = tk.Frame(top)
        bar.pack(fill="x", padx=8, pady=8)
        tk.Label(bar, text="Search:").pack(side="left")
        entry_box = tk.Entry(bar, textvariable=search_var)
        entry_box.pack(side="left", fill="x", expand=True, padx=(4, 8))
        kind_menu = ttk.Combobox(
            bar,
            textvariable=kind_var,
            state="readonly",
            values=["all", KIND_DICTATION, KIND_COMMAND],
            width=10,
        )
        kind_menu.pack(side="left")
        entry_box.focus_set()

        columns = ("time", "kind", "preview")
        tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="browse")
        tree.heading("time", text="Time")
        tree.heading("kind", text="Kind")
        tree.heading("preview", text="Text")
        tree.column("time", width=150, stretch=False)
        tree.column("kind", width=90, stretch=False)
        tree.column("preview", width=480)
        tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._tree = tree

        detail = tk.Text(top, height=6, wrap="word")
        detail.pack(fill="both", padx=8, pady=(0, 8))
        detail.configure(state="disabled")
        self._detail = detail

        button_bar = tk.Frame(top)
        button_bar.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(button_bar, text="Copy", command=self._copy_selected).pack(side="left")
        tk.Button(button_bar, text="Delete", command=self._delete_selected).pack(
            side="left", padx=(6, 0)
        )
        tk.Button(button_bar, text="Refresh", command=self._refresh).pack(side="right")

        search_var.trace_add("write", lambda *_args: self._refresh())
        kind_menu.bind("<<ComboboxSelected>>", lambda _e: self._refresh())
        tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

        self._refresh()

    def _refresh(self) -> None:
        if self._tree is None or self._search_var is None or self._kind_var is None:
            return
        tree = self._tree
        for row in tree.get_children():
            tree.delete(row)
        self._entries_by_row.clear()

        kind = None if self._kind_var.get() == "all" else self._kind_var.get()
        try:
            entries = self._store.search(self._search_var.get(), kind=kind, limit=300)
        except Exception:
            logger.exception("could not search history")
            entries = []

        for entry in entries:
            preview = entry.text.replace("\n", " ")[:_PREVIEW_CHARS]
            row_id = tree.insert(
                "", "end", values=(_format_time(entry.created_at), entry.kind, preview)
            )
            self._entries_by_row[row_id] = entry
        self._show_detail()

    def _selected_entry(self) -> HistoryEntry | None:
        if self._tree is None:
            return None
        selection = self._tree.selection()
        if not selection:
            return None
        return self._entries_by_row.get(selection[0])

    def _show_detail(self) -> None:
        if self._detail is None:
            return
        entry = self._selected_entry()
        self._detail.configure(state="normal")
        self._detail.delete("1.0", "end")
        if entry is not None:
            text = entry.text
            if entry.instruction:
                text = f"Instruction: {entry.instruction}\n\n{text}"
            self._detail.insert("1.0", text)
        self._detail.configure(state="disabled")

    def _copy_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._toplevel is None:
            return
        # Tk's own clipboard methods: this window is only ever touched from
        # the Tk thread, so there's no need to route through PlatformAdapter.
        self._toplevel.clipboard_clear()
        self._toplevel.clipboard_append(entry.text)

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            self._store.delete(entry.id)
        except Exception:
            logger.exception("could not delete history entry")
            return
        self._refresh()


def _format_time(epoch_s: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch_s))


__all__ = ["HistoryWindow"]
