"""TVDB search dialog, shared by the naming step and `rne edit`.

Dismisses with a tvdb.Match (picked), TYPED ("use the name as typed"), or
None (cancelled).
"""

from __future__ import annotations

import asyncio
from typing import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from rne.tui.common import Plain, cell
from rne.tvdb import Match, TvdbError

Lookup = Callable[[str, str], list[Match]]  # (query, "series" | "movie") -> matches

TYPED = "typed"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


class LookupModal(ModalScreen[Match | str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+t", "typed", "Use as typed"),
    ]
    DEFAULT_CSS = """
    LookupModal { align: center middle; }
    LookupModal > Vertical { width: 90%; max-width: 140; height: auto; max-height: 90%;
                             border: thick $accent; background: $surface; padding: 1 2; }
    LookupModal DataTable { height: auto; max-height: 16; margin-top: 1; }
    LookupModal #status { color: $text-muted; margin-top: 1; }
    LookupModal #overview { color: $text-muted; margin-top: 1; height: auto; max-height: 4; }
    LookupModal Horizontal { height: auto; margin-top: 1; }
    LookupModal Button { margin-right: 2; }
    """

    def __init__(self, lookup: Lookup, query: str, kind: str) -> None:
        super().__init__()
        self.lookup = lookup
        self.query_text = query
        self.kind = kind  # "series" or "movie"
        self.matches: list[Match] = []

    def compose(self) -> ComposeResult:
        what = "TV series" if self.kind == "series" else "movie"
        with Vertical():
            yield Plain(f"Find the {what} on TVDB", classes="section")
            yield Input(self.query_text, placeholder="name to search for", id="query")
            yield Plain("", id="status")
            yield DataTable(cursor_type="row", zebra_stripes=True, id="results")
            yield Plain("", id="overview")
            with Horizontal():
                yield Button("Use selected  enter", variant="primary", id="use", compact=True,
                             disabled=True)
                yield Button("Use name as typed  ctrl+t", id="typed", compact=True)
                yield Button("Cancel  esc", id="cancel", compact=True)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Name", "Year", "Network", "Country")
        self.search(self.query_text)

    @work(exclusive=True)
    async def search(self, query: str) -> None:
        status = self.query_one("#status", Static)
        table = self.query_one(DataTable)
        status.update(f"Searching TVDB for {query!r} ...")
        table.clear()
        self.matches = []
        self.query_one("#use", Button).disabled = True
        try:
            self.matches = await asyncio.to_thread(self.lookup, query, self.kind)
        except TvdbError as exc:
            status.update(f"✗ {exc}. Edit the name and press Enter to retry, "
                          "or use the name as typed.")
            return
        if not self.matches:
            status.update("No matches. Edit the name and press Enter to search again.")
            self.query_one("#query", Input).focus()
            return
        for i, m in enumerate(self.matches):
            table.add_row(cell(_clip(m.name, 60)), str(m.year or ""),
                          cell(_clip(m.network, 24)), cell(m.country), key=str(i))
        status.update(f"{len(self.matches)} match(es). ↑↓ to choose, Enter to use.")
        self.query_one("#use", Button).disabled = False
        table.focus()
        self._show_overview(0)

    def _show_overview(self, index: int) -> None:
        if 0 <= index < len(self.matches):
            self.query_one("#overview", Static).update(_clip(self.matches[index].overview, 400))

    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_overview(event.cursor_row)

    @on(Input.Submitted, "#query")
    def _search_again(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.search(event.value.strip())

    @on(DataTable.RowSelected)
    @on(Button.Pressed, "#use")
    def _use(self) -> None:
        table = self.query_one(DataTable)
        if self.matches and 0 <= table.cursor_row < len(self.matches):
            self.dismiss(self.matches[table.cursor_row])

    @on(Button.Pressed, "#typed")
    def action_typed(self) -> None:
        self.dismiss(TYPED)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
