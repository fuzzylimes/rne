"""`rne edit`: change a queued job's (or a whole batch's) naming and settings."""

from __future__ import annotations

import sqlite3

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from rne import jobs
from rne.models import JobStatus
from rne.tui.common import APP_CSS
from rne.tui.nav import FORM_BINDINGS, NAV_HINT
from rne.tui.settings import SettingsForm



def _picker_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Editable jobs: the queue first (in run order), then recent history."""
    return conn.execute(
        """
        SELECT j.*, b.label AS batch_label
        FROM   jobs j LEFT JOIN ingest_batches b ON b.id = j.ingest_batch_id
        WHERE  j.status != 'running'
        ORDER BY CASE WHEN j.status IN ('queued', 'paused') THEN 0 ELSE 1 END,
                 CASE WHEN j.status IN ('queued', 'paused') THEN j.priority END,
                 CASE WHEN j.status IN ('queued', 'paused') THEN j.id END,
                 j.id DESC
        LIMIT  300
        """
    ).fetchall()


class PickerScreen(Screen):
    """Choose a job (Enter) or its whole batch (b) to edit."""

    DEFAULT_CSS = "PickerScreen #jobs { max-height: 100%; }"

    BINDINGS = [
        Binding("enter", "edit_job", "Edit job", priority=True),
        Binding("b", "edit_batch", "Edit whole batch"),
        Binding("escape,q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Pick a job to edit. Running jobs are not listed.", classes="hint")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="jobs")
        yield Footer()

    def on_mount(self) -> None:
        self.app.sub_title = "pick a job"
        table = self.query_one(DataTable)
        table.add_columns("ID", "Status", "Job", "Title", "Batch")
        for row in _picker_rows(self.app.conn):  # type: ignore[attr-defined]
            is_tv = row["show"] is not None
            label = f"S{row['season']:02d}E{row['episode']:02d}" if is_tv else "movie"
            table.add_row(
                str(row["id"]), row["status"], label, row["show"] or row["movie"],
                row["batch_label"] or "",
                key=f"{row['id']}:{row['ingest_batch_id'] or ''}",
            )
        if table.row_count == 0:
            self.query_one(".hint", Static).update("No editable jobs.")

    def _selected(self) -> tuple[int, int | None] | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        job_id, _, batch = str(key).partition(":")
        return int(job_id), int(batch) if batch else None

    def action_edit_job(self) -> None:
        sel = self._selected()
        if sel:
            self.app.open_editor([sel[0]])  # type: ignore[attr-defined]

    def action_edit_batch(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        if sel[1] is None:
            self.notify("This job has no batch.", severity="warning")
            return
        self.app.open_editor(jobs.batch_job_ids(self.app.conn, sel[1]))  # type: ignore[attr-defined]


class EditScreen(Screen):
    """Naming plus track/encode settings for one job or a batch of jobs."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel"),
        *FORM_BINDINGS,
    ]

    def __init__(self, targets: list[jobs.EditTarget]) -> None:
        super().__init__()
        self.targets = targets
        self.first = targets[0]
        self.batch = len(targets) > 1

    # -- layout ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        t = self.first
        yield Header()
        with VerticalScroll(id="body"):
            yield Static(self._heading(), id="heading")
            yield Static("Naming", classes="section")
            with Horizontal(classes="field"):
                yield Label("Show" if t.is_tv else "Movie")
                yield Input(t.title, id="title", compact=True)
            if t.is_tv:
                with Horizontal(classes="field"):
                    yield Label("Season")
                    yield Input(str(t.season), type="integer", id="season", classes="number", compact=True)
                first_ep = min(x.episode or 0 for x in self.targets)
                with Horizontal(classes="field"):
                    yield Label("First episode" if self.batch else "Episode")
                    yield Input(str(first_ep), type="integer", id="episode", classes="number", compact=True)
            yield Static("", id="outputs")

            problems = [x for x in self.targets if x.summary is None]
            if problems:
                yield Static(
                    "\n".join(f"⚠ {x.label}: {x.probe_error}" for x in problems)
                    + "\nShowing the saved settings. Tracks can't be listed or checked "
                    "without the file, but everything can still be changed.",
                    classes="warn",
                )
            readable = next((x for x in self.targets if x.summary is not None), None)
            # Tracks come from the first readable source; settings from the first job.
            yield SettingsForm(readable.summary if readable else None, t.args, id="settings")
            if self.batch:
                yield Static(
                    f"Settings apply to all {len(self.targets)} jobs; each keeps its "
                    "own chapter range.",
                    classes="hint",
                )

            yield Static("", id="errors")
            yield Static(NAV_HINT, classes="hint")
            with Horizontal(id="buttons"):
                yield Button("Save  ctrl+s", variant="primary", id="save", compact=True)
                yield Button("Cancel  esc", id="cancel", compact=True)
        yield Footer()

    def _heading(self) -> str:
        if not self.batch:
            t = self.first
            note = "  (done: retry it to re-encode with new settings)" if t.status == "done" else ""
            return f"Job {t.job_id} · {t.label} · {t.title} · {t.status}{note}"
        statuses = sorted({t.status for t in self.targets})
        return (f"Batch of {len(self.targets)} jobs · {self.first.title} · "
                f"{', '.join(statuses)}")

    def on_mount(self) -> None:
        self.app.sub_title = "edit"
        self.refresh_plan()

    # -- live preview and validation ---------------------------------------------

    def _int(self, widget_id: str) -> int | None:
        widgets = self.query(f"#{widget_id}")
        if not widgets:
            return None
        text = widgets.first(Input).value.strip()
        return int(text) if text.lstrip("-").isdigit() else None

    def current_plan(self) -> tuple[list[jobs.EditResult], list[str]]:
        errors: list[str] = []
        title = self.query_one("#title", Input).value.strip()
        if not title:
            errors.append("name can't be empty")
        season = episode = None
        if self.first.is_tv:
            season, episode = self._int("season"), self._int("episode")
            if season is None or season < 0:
                errors.append("season must be 0 or more")
            if episode is None or episode < 1:
                errors.append("episode must be 1 or more")
        template = None
        forms = self.query(SettingsForm)
        if forms:
            form = forms.first()
            try:
                template = form.value()
            except ValueError as exc:
                errors.append(str(exc))
                return [], errors
        if errors:
            return [], errors
        results = jobs.plan_edits(
            self.targets, template=template, title=title, season=season, first_episode=episode
        )
        for r in results:
            errors += r.errors
        return results, errors

    def refresh_plan(self) -> None:
        results, errors = self.current_plan()
        lines = [f"{r.label:<8} → {r.output_path}" for r in results]
        self.query_one("#outputs", Static).update("\n".join(lines))
        self.query_one("#errors", Static).update("\n".join(f"✗ {e}" for e in errors))
        self.query_one("#save", Button).disabled = bool(errors)

    @on(Input.Changed)
    @on(SettingsForm.Changed)
    def _changed(self) -> None:
        self.refresh_plan()

    # -- actions -------------------------------------------------------------------

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        results, errors = self.current_plan()
        if errors:
            self.notify("Fix the errors first.", severity="error")
            return
        self.app.exit(jobs.apply_edits(self.app.conn, results))  # type: ignore[attr-defined]

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if self.app.picker:  # type: ignore[attr-defined]
            self.app.pop_screen()
        else:
            self.app.exit((["Edit cancelled; nothing changed."], True))


class EditApp(App[tuple[list[str], bool]]):
    """Returns (lines to print, whether every change was saved)."""

    CSS = APP_CSS
    TITLE = "rne"

    def __init__(self, conn: sqlite3.Connection, job_ids: list[int] | None = None) -> None:
        super().__init__()
        self.conn = conn
        self.job_ids = job_ids
        self.picker = job_ids is None

    def on_mount(self) -> None:
        if self.picker:
            self.push_screen(PickerScreen())
        else:
            self.open_editor(self.job_ids or [])

    def open_editor(self, job_ids: list[int]) -> None:
        if not job_ids:
            self.notify("Nothing to edit.", severity="warning")
            return
        targets = jobs.load_edit_targets(self.conn, job_ids)
        running = [t for t in targets if t.status == JobStatus.RUNNING]
        if running:
            self.notify(f"Job {running[0].job_id} is running; stop the worker to edit it.",
                        severity="error")
            if not self.picker:
                self.exit(([f"Job {running[0].job_id} is currently running. "
                            "Stop the worker before editing."], False))
            return
        kinds = {t.is_tv for t in targets}
        if len(kinds) > 1:
            self.notify("Batch mixes TV and movie jobs; edit them one at a time.",
                        severity="error")
            return
        self.push_screen(EditScreen(targets))

