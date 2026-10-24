"""`rne ingest` and `rne queue` as a full-screen app.

Steps (queue skips Titles and Rip):

    Titles → Naming → Rip → Tracks → [Chapters] → Review

Every step reads and writes the shared Draft, so going back keeps what was
entered. Once the rip has started there is no going back to Titles or Rip;
naming can still be changed, and outputs follow it.
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from typing import Callable

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
    RadioButton,
    RadioSet,
    Static,
)

from rne import jobs, makemkv, plan, probe
from rne.cli._pipeline import _audio_summary, _subtitle_summary, mungefilename
from rne.cli.disc_split import autodetect, fixed_split, groups_to_episodes, parse_ranges
from rne.models import HandbrakeArgs
from rne.tui.common import APP_CSS, Plain, cell
from rne.tui.flow import Draft, Source
from rne.tui.lookup import TYPED, Lookup, LookupModal
from rne.tui.nav import FORM_BINDINGS, NAV_HINT, NavRadioSet
from rne.tui.settings import SettingsForm

DEFAULT_EPISODE_MINUTES = 24

Ripper = Callable[[int, pathlib.Path, Callable[[makemkv.RipEvent], None]], pathlib.Path]
Prober = Callable[[pathlib.Path], probe.StreamSummary]
ChapterProber = Callable[[pathlib.Path], list[probe.Chapter]]
Notifier = Callable[..., str | None]


def _fmt_seconds(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _int_or_none(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


class StepScreen(Screen):
    """Base for the wizard steps."""

    STEP = ""
    BINDINGS = FORM_BINDINGS

    @property
    def draft(self) -> Draft:
        return self.app.draft  # type: ignore[attr-defined]

    @property
    def flow(self) -> PipelineApp:
        return self.app  # type: ignore[return-value]

    def on_mount(self) -> None:
        self.app.sub_title = self.flow.step_trail(self.STEP)


# ---------------------------------------------------------------------------
# Titles (ingest)
# ---------------------------------------------------------------------------


class TitlesScreen(StepScreen):
    STEP = "Titles"
    BINDINGS = [
        Binding("space", "toggle", "Select"),
        Binding("a", "toggle_all", "All / none"),
        Binding("enter", "next", "Next", priority=True),
    ]
    DEFAULT_CSS = "TitlesScreen DataTable { max-height: 100%; }"

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Plain(f"Disc: {self.flow.volume_name}", classes="section")
            yield Plain("Space selects a title, a selects all. Titles rip top to bottom, "
                         "and episodes are numbered in that order.", classes="hint")
            yield DataTable(cursor_type="row", zebra_stripes=True, id="titles")
            yield Plain("", id="errors")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("✓", key="sel")
        for col in ("#", "Disc Index", "Source", "Duration", "Size", "Ch", "Resolution",
                    "FPS", "Audio"):
            table.add_column(col)
        for display_idx, disc_idx in enumerate(self.flow.display_order):
            row = makemkv.summarize(disc_idx, self.flow.titles[disc_idx])
            mark = "✓" if disc_idx in self.draft.selected_titles else " "
            table.add_row(mark, str(display_idx), str(disc_idx),
                          *(cell(row[c]) for c in ("Source", "Duration", "Size", "Ch",
                                                    "Resolution", "FPS", "Audio")),
                          key=str(disc_idx))
        table.focus()

    def _set(self, disc_idx: int, on_: bool) -> None:
        selected = set(self.draft.selected_titles)
        selected.add(disc_idx) if on_ else selected.discard(disc_idx)
        # Keep display (.mpls) order, which is the rip and episode order.
        self.draft.selected_titles = [i for i in self.flow.display_order if i in selected]
        self.query_one(DataTable).update_cell(str(disc_idx), "sel", "✓" if on_ else " ")
        self.query_one("#errors", Static).update("")

    def action_toggle(self) -> None:
        table = self.query_one(DataTable)
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        disc_idx = int(str(key))
        self._set(disc_idx, disc_idx not in self.draft.selected_titles)

    def action_toggle_all(self) -> None:
        on_ = len(self.draft.selected_titles) < len(self.flow.display_order)
        for disc_idx in self.flow.display_order:
            self._set(disc_idx, on_)

    def action_next(self) -> None:
        if not self.draft.selected_titles:
            self.query_one("#errors", Static).update("✗ select at least one title")
            return
        if len(self.draft.selected_titles) != 1:
            self.draft.naming.disc_split = False
        self.flow.goto("naming")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


class NamingScreen(StepScreen):
    STEP = "Naming"
    BINDINGS = [
        Binding("ctrl+n", "next", "Next", priority=True),
        Binding("ctrl+f", "lookup", "Look up on TVDB"),
        Binding("escape", "back", "Back"),
    ]
    DEFAULT_CSS = """
    NamingScreen #match { padding-left: 16; }
    NamingScreen NavRadioSet { layout: horizontal; height: auto; width: auto; border: none;
                            padding: 0; }
    NamingScreen NavRadioSet > RadioButton { width: auto; margin-right: 4; }
    NamingScreen .tv-only.hidden { display: none; }
    """

    def compose(self) -> ComposeResult:
        n = self.draft.naming
        locked = self.flow.ripped  # after the rip, the kind and split mode are fixed
        yield Header()
        with VerticalScroll(id="body"):
            if self.draft.mode == "queue":
                yield Plain("Files", classes="section")
                yield Plain("\n".join(f"{i}. {s.path.name}"
                                       for i, s in enumerate(self.draft.sources, 1)),
                             classes="muted")
            yield Plain("What is it?", classes="section")
            with NavRadioSet(id="kind", disabled=locked):
                yield RadioButton("TV episodes", value=n.is_tv, id="tv")
                yield RadioButton("Movie", value=not n.is_tv, id="movie")
            with Horizontal(classes="field"):
                yield Label("Name")
                yield Input(n.title, id="title", compact=True)
            yield Plain("", id="match", classes="muted")
            with Horizontal(classes="field"):
                yield Label("Year")
                yield Input("" if n.year is None else str(n.year), type="integer",
                            placeholder="optional", id="year", classes="number", compact=True)
                yield Plain("adds (year) to the folder name", classes="muted")
            with Horizontal(classes="field tv-only"):
                yield Label("Season")
                yield Input("" if n.season is None else str(n.season), type="integer",
                            id="season", classes="number", compact=True)
                yield Plain("0 for specials", classes="muted")
            with Horizontal(classes="field tv-only"):
                yield Label("First episode")
                yield Input("" if n.first_episode is None else str(n.first_episode),
                            type="integer", id="first", classes="number", compact=True)
            if self._file_count() == 1:
                yield Checkbox("One file holds several episodes (split by chapters)",
                               n.disc_split, id="split", classes="tv-only", compact=True,
                               disabled=locked)
            yield Plain("", id="preview", classes="ok")
            yield Plain("", id="errors")
            yield Plain(NAV_HINT, classes="hint")
            with Horizontal(id="buttons"):
                yield Button("Back  esc", id="back", compact=True,
                             disabled=self._back_target() is None)
                yield Button("Next  ctrl+n", variant="primary", id="next", compact=True)
        yield Footer()

    def _back_target(self) -> str | None:
        if self.draft.mode == "ingest":
            return "tracks" if self.flow.ripped else "titles"
        return "tracks" if self.draft.reference_summary is not None else None

    def _file_count(self) -> int:
        if self.draft.mode == "ingest":
            return len(self.draft.selected_titles)
        return len(self.draft.sources)

    def on_mount(self) -> None:
        n = self.draft.naming
        # The name a TVDB match was made for; editing the name drops the match.
        self._matched_title: str | None = n.title if n.tvdb_id else None
        # The name the user last looked up (or chose to keep as typed).
        self._looked_up: str | None = n.title if n.tvdb_id else None
        self.query_one("#title", Input).focus()
        self._refresh()

    def _read(self) -> None:
        n = self.draft.naming
        n.is_tv = self.query_one("#tv", RadioButton).value
        n.title = self.query_one("#title", Input).value
        n.year = _int_or_none(self.query_one("#year", Input).value)
        if n.tvdb_id is not None and n.title != self._matched_title:
            n.tvdb_id = None
        n.season = _int_or_none(self.query_one("#season", Input).value)
        n.first_episode = _int_or_none(self.query_one("#first", Input).value)
        splits = self.query("#split")
        n.disc_split = n.is_tv and bool(splits) and splits.first(Checkbox).value

    def _refresh(self) -> None:
        self._read()
        n = self.draft.naming
        for w in self.query(".tv-only"):
            w.set_class(not n.is_tv, "hidden")
        errors = n.errors()
        preview = ""
        if not errors:
            if n.is_tv and n.disc_split:
                preview = (f"→ episodes from S{n.season:02d}E{n.first_episode:02d}, "
                           "split by chapters after the encode settings")
            elif n.is_tv:
                # Before the rip, number the selected titles; after it, the real
                # numbers (a skipped title leaves a gap).
                count = None if self.draft.sources else self._file_count()
                preview = "→ " + self.draft.episode_preview(count)
            else:
                preview = f"→ {n.folder}.mkv"
        if n.clean_title:
            preview += f"\n   in {self.draft.staging_dir}"
        self.query_one("#preview", Static).update(preview)
        self.query_one("#match", Static).update(self._match_line())
        self.query_one("#errors", Static).update("\n".join(f"✗ {e}" for e in errors))
        self.query_one("#next", Button).disabled = bool(errors)

    def _match_line(self) -> str:
        n = self.draft.naming
        if n.tvdb_id is not None:
            return f"✓ TVDB #{n.tvdb_id}"
        if self.flow.lookup is None:
            return f"TVDB lookup off: {self.flow.lookup_note}" if self.flow.lookup_note else ""
        return "Enter (or ctrl+f) looks the name up on TVDB"

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "lookup":
            return self.flow.lookup is not None
        return True

    @on(Input.Changed)
    @on(RadioSet.Changed)
    @on(Checkbox.Changed)
    def _changed(self) -> None:
        self._refresh()

    @on(Input.Submitted, "#title")
    def _title_submitted(self) -> None:
        # First Enter on a new name searches TVDB; the next one moves on.
        self._read()
        if self.flow.lookup is not None and self._looked_up != self.draft.naming.title:
            self.action_lookup()
        else:
            self.action_next()

    def action_lookup(self) -> None:
        self._read()
        n = self.draft.naming
        if self.flow.lookup is None or not n.title.strip():
            return
        self._looked_up = n.title
        kind = "series" if n.is_tv else "movie"
        self.app.push_screen(LookupModal(self.flow.lookup, n.title.strip(), kind),
                             self._apply_match)

    def _apply_match(self, result) -> None:
        n = self.draft.naming
        title = self.query_one("#title", Input)
        if result is None:
            title.focus()
            return
        if result == TYPED:
            n.tvdb_id = None
        else:
            clean = mungefilename(result.name)
            self._matched_title = self._looked_up = clean
            n.tvdb_id = result.tvdb_id
            title.value = clean
            self.query_one("#year", Input).value = str(result.year or "")
        self._refresh()
        # On to the next thing that needs answering.
        target = "#season" if n.is_tv and n.season is None else (
            "#first" if n.is_tv and n.first_episode is None else "#next")
        self.query_one(target).focus()

    @on(Input.Submitted, "#year, #season, #first")
    @on(Button.Pressed, "#next")
    def action_next(self) -> None:
        self._refresh()
        if self.draft.naming.errors():
            return
        if self.draft.mode == "queue":
            self.flow.probe_then("tracks")  # probes only what isn't probed yet
        elif self.flow.ripped:
            self.flow.goto("tracks")
        else:
            self.flow.goto("rip")

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self._read()
        target = self._back_target()
        if target:
            self.flow.goto(target)


# ---------------------------------------------------------------------------
# Rip (ingest)
# ---------------------------------------------------------------------------


class RipFailedModal(ModalScreen[str]):
    """Abort the ingest, retry the title, or skip it."""

    BINDINGS = [Binding("a", "pick('abort')"), Binding("r", "pick('retry')"),
                Binding("s", "pick('skip')")]
    DEFAULT_CSS = """
    RipFailedModal { align: center middle; }
    RipFailedModal > Vertical { width: 80; height: auto; border: thick $error;
                                background: $surface; padding: 1 2; }
    RipFailedModal Horizontal { height: auto; margin-top: 1; }
    RipFailedModal Button { margin-right: 2; }
    """

    def __init__(self, title_idx: int, error: str) -> None:
        super().__init__()
        self.title_idx = title_idx
        self.error = error

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Plain(f"Title {self.title_idx} failed to rip", classes="section")
            yield Plain(self.error)
            with Horizontal():
                yield Button("Retry (r)", variant="primary", id="retry", compact=True)
                yield Button("Skip title (s)", id="skip", compact=True)
                yield Button("Abort ingest (a)", variant="error", id="abort", compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_pick(self, choice: str) -> None:
        self.dismiss(choice)


class RipScreen(StepScreen):
    STEP = "Rip"
    _title_label = ""
    BINDINGS = [Binding("escape", "back", "Back")]
    DEFAULT_CSS = """
    RipScreen ProgressBar { margin: 0 0 1 0; }
    RipScreen ProgressBar Bar { width: 80; }
    RipScreen Log { height: 12; border: round $panel; scrollbar-size-horizontal: 0; }
    RipScreen .running-only.hidden { display: none; }
    """

    def compose(self) -> ComposeResult:
        d = self.draft
        yield Header()
        with VerticalScroll(id="body"):
            yield Plain(f"{len(d.selected_titles)} title(s) to rip, in this order:",
                         classes="section")
            yield Plain("\n".join(self.flow.title_line(t) for t in d.selected_titles),
                         classes="muted")
            with Horizontal(classes="field"):
                yield Label("Rip into")
                yield Input(str(d.staging_dir), id="staging", compact=True)
            yield Plain("", id="raw", classes="muted")
            with Vertical(classes="running-only hidden", id="progress"):
                yield Plain("", id="current")
                yield ProgressBar(total=100, show_eta=False, id="title-bar")
                yield Plain("", id="overall-label")
                yield ProgressBar(total=len(d.selected_titles), show_eta=False,
                                  id="overall-bar")
                yield Log(id="log", max_lines=500)
            yield Plain("", id="errors")
            with Horizontal(id="buttons"):
                yield Button("Back  esc", id="back", compact=True)
                yield Button("Start rip", variant="primary", id="start", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_raw()
        self.query_one("#start", Button).focus()

    def _staging(self) -> pathlib.Path:
        return pathlib.Path(self.query_one("#staging", Input).value.strip()).expanduser()

    def _refresh_raw(self) -> None:
        self.query_one("#raw", Static).update(
            f"Raw files go to {self._staging()}/_raw/batch-<n>/ "
            "(the batch number is assigned when the rip starts)"
        )

    @on(Input.Changed, "#staging")
    def _staging_changed(self) -> None:
        self._refresh_raw()

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        if not self.flow.ripping:
            self.flow.goto("naming")

    @on(Button.Pressed, "#start")
    def _start(self) -> None:
        staging = self._staging()
        if not staging.is_absolute():
            self.query_one("#errors", Static).update("✗ use an absolute path")
            return
        if staging != self.draft.staging_root / self.draft.naming.clean_title:
            self.draft.staging_override = staging
        self.flow.ripping = True
        self.query_one("#staging", Input).disabled = True
        self.query_one("#back", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self.query_one("#progress").remove_class("hidden")
        self.rip()

    def _on_event(self, event: makemkv.RipEvent) -> None:
        if event.kind == "progress":
            self.query_one("#title-bar", ProgressBar).update(progress=event.fraction * 100)
        elif event.kind == "task":
            self.query_one("#current", Static).update(f"{self._title_label}: {event.text}")
        else:
            self.query_one("#log", Log).write_line(event.text)

    @work(exclusive=True)
    async def rip(self) -> None:
        flow, d = self.flow, self.draft
        log = self.query_one("#log", Log)
        try:
            raw_dir = flow.start_batch()
        except OSError as exc:
            flow.exit_with([f"Cannot create the raw folder: {exc}"], ok=False)
            return
        self.query_one("#raw", Static).update(f"Raw files go to {raw_dir}/")
        total = len(d.selected_titles)
        for n, title_idx in enumerate(d.selected_titles, 1):
            self._title_label = f"{flow.title_line(title_idx)} ({n} of {total})"
            self.query_one("#overall-label", Static).update(
                f"{n - 1} of {total} done" + (f", {len(d.sources)} ripped" if d.sources else ""))
            attempts = 0
            while True:
                self.query_one("#title-bar", ProgressBar).update(progress=0)
                self.query_one("#current", Static).update(f"{self._title_label}: starting")
                try:
                    path = await asyncio.to_thread(
                        flow.ripper, title_idx, raw_dir,
                        lambda e: flow.call_from_thread(self._on_event, e),
                    )
                except (makemkv.MakemkvError, OSError) as exc:
                    log.write_line(f"✗ {exc}")
                    if attempts < flow.auto_retries:
                        attempts += 1
                        log.write_line(f"Retrying ({attempts} of {flow.auto_retries}) ...")
                        continue
                    choice = await flow.push_screen_wait(RipFailedModal(title_idx, str(exc)))
                    if choice == "abort":
                        flow.exit_with([f"Aborted. Raw files so far are in {raw_dir}/"],
                                       ok=False)
                        return
                    if choice == "retry":
                        continue
                    log.write_line(f"Skipped title {title_idx}.")
                    break
                d.sources.append(Source(path=path, title_idx=title_idx))
                log.write_line(f"✓ Title {title_idx} → {path.name}")
                break
            self.query_one("#overall-bar", ProgressBar).update(progress=n)
        if not d.sources:
            flow.exit_with(["No titles survived. Aborting."], ok=False)
            return
        flow.ripped = True
        flow.ripping = False
        message = flow.notify_rip_complete()
        if message:
            log.write_line(message)
        self.query_one("#current", Static).update(f"Ripped {len(d.sources)} of {total}. "
                                                  "Reading tracks ...")
        if await flow.probe_sources():
            flow.goto("tracks")


# ---------------------------------------------------------------------------
# Tracks & encode settings
# ---------------------------------------------------------------------------


def _video_line(summary: probe.StreamSummary) -> str:
    if not summary.video:
        return "no video stream"
    v = summary.video[0]
    return " ".join(x for x in (v.codec, v.resolution, f"{v.fps} fps" if v.fps else "",
                                v.field_order) if x)


class TracksScreen(StepScreen):
    """Settings for every job, or (override=path) for one file."""

    STEP = "Tracks"
    BINDINGS = [
        Binding("ctrl+n", "next", "Next", priority=True),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, override: pathlib.Path | None = None) -> None:
        super().__init__()
        self.override = override

    def compose(self) -> ComposeResult:
        d = self.draft
        path = self.override or d.reference.path  # type: ignore[union-attr]
        summary = d.summaries[path]
        if self.override:
            args = d.overrides.get(path) or d.template
            if args is None or plan.validate(args, summary):
                # The shared settings don't fit this file: start from its own defaults.
                args = plan.default_args(summary, d.is_dvd)
            heading = f"Settings for {path.name} only"
        else:
            args = d.template or d.default_template()
            heading = f"Settings for all jobs (tracks read from {path.name})"
        yield Header()
        with VerticalScroll(id="body"):
            yield Plain(heading, classes="section")
            yield Plain(_video_line(summary), classes="muted")
            yield SettingsForm(summary, args, is_dvd=d.is_dvd, id="settings")
            yield Plain("", id="errors")
            yield Plain(NAV_HINT, classes="hint")
            with Horizontal(id="buttons"):
                if self.override:
                    yield Button("Cancel  esc", id="back", compact=True)
                    yield Button("Use the shared settings", id="reset", compact=True)
                    yield Button("Save  ctrl+n", variant="primary", id="next", compact=True)
                else:
                    yield Button("Back  esc", id="back", compact=True)
                    yield Button("Next  ctrl+n", variant="primary", id="next", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> list[str]:
        errors = self.query_one(SettingsForm).errors()
        self.query_one("#errors", Static).update("\n".join(f"✗ {e}" for e in errors))
        self.query_one("#next", Button).disabled = bool(errors)
        return errors

    @on(SettingsForm.Changed)
    def _changed(self) -> None:
        self._refresh()

    @on(Button.Pressed, "#next")
    def action_next(self) -> None:
        if self._refresh():
            return
        args = self.query_one(SettingsForm).value()
        if self.override:
            self.draft.overrides[self.override] = args
            self.flow.goto("review")
        else:
            self.draft.template = args
            self.flow.goto("chapters" if self.draft.naming.disc_split else "review")

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        self.draft.overrides.pop(self.override, None)  # type: ignore[arg-type]
        self.flow.goto("review")

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        if self.override:
            self.flow.goto("review")
            return
        try:  # keep what was picked when stepping back
            self.draft.template = self.query_one(SettingsForm).value()
        except ValueError:
            pass
        self.flow.goto("naming")


# ---------------------------------------------------------------------------
# Chapters (multi-episode file)
# ---------------------------------------------------------------------------


class ChaptersScreen(StepScreen):
    STEP = "Chapters"
    BINDINGS = [
        Binding("ctrl+n", "next", "Next", priority=True),
        Binding("escape", "back", "Back"),
    ]
    DEFAULT_CSS = """
    ChaptersScreen #tables { height: auto; }
    ChaptersScreen #tables DataTable { width: 1fr; margin-right: 2; }
    ChaptersScreen .field Input { width: 24; }
    ChaptersScreen .field Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="body"):
            yield Plain("Split one file into episodes", classes="section")
            yield Plain("Short chapters (openings, previews) are folded into the nearest "
                         "episode.", classes="hint")
            with Horizontal(id="tables"):
                yield DataTable(cursor_type="none", id="chapters")
                yield DataTable(cursor_type="none", id="episodes")
            with Horizontal(classes="field"):
                yield Label("Episode length")
                yield Input(str(DEFAULT_EPISODE_MINUTES), id="minutes", compact=True)
                yield Button("Split by length", id="by-length", compact=True)
            with Horizontal(classes="field"):
                yield Label("Chapters each")
                yield Input("", type="integer", id="fixed", compact=True)
                yield Button("Split evenly", id="by-count", compact=True)
            with Horizontal(classes="field"):
                yield Label("Ranges")
                yield Input("", placeholder="e.g. 1-6, 7-12, 13-18", id="ranges", compact=True)
                yield Button("Use ranges", id="by-ranges", compact=True)
            yield Plain("", id="errors")
            with Horizontal(id="buttons"):
                yield Button("Back  esc", id="back", compact=True)
                yield Button("Next  ctrl+n", variant="primary", id="next", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        chapters = self.query_one("#chapters", DataTable)
        chapters.add_columns("#", "Title", "Duration", "Running")
        episodes = self.query_one("#episodes", DataTable)
        episodes.add_columns("Episode", "Chapters", "Duration")
        self.load()

    @work(exclusive=True)
    async def load(self) -> None:
        d = self.draft
        if d.chapters is None:
            self.query_one("#errors", Static).update("Reading chapters ...")
            try:
                d.chapters = await asyncio.to_thread(self.flow.chapter_prober, d.reference.path)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self.query_one("#errors", Static).update(f"✗ cannot read chapters: {exc}")
                return
            self.query_one("#errors", Static).update("")
        if not d.chapters:
            self.query_one("#errors", Static).update("✗ this file has no chapters")
            return
        if d.episodes is None:
            self._split_by_length(DEFAULT_EPISODE_MINUTES)
        table = self.query_one("#chapters", DataTable)
        table.clear()
        running = 0.0
        for ch in d.chapters:
            running += ch.duration
            table.add_row(str(ch.number), cell(ch.title), _fmt_seconds(ch.duration),
                          _fmt_seconds(running))
        self._show_episodes()

    def _start_ep(self) -> int:
        return self.draft.naming.first_episode or 1

    def _split_by_length(self, minutes: float) -> None:
        d = self.draft
        d.episodes = groups_to_episodes(autodetect(d.chapters, minutes * 60), self._start_ep())  # type: ignore[arg-type]

    def _show_episodes(self) -> None:
        table = self.query_one("#episodes", DataTable)
        table.clear()
        season = self.draft.naming.season or 0
        for ep in self.draft.episodes or []:
            table.add_row(f"S{season:02d}E{ep.number:02d}",
                          f"{ep.chapter_start}-{ep.chapter_end}", ep.duration_str())
        self.query_one("#next", Button).disabled = not self.draft.episodes

    def _error(self, text: str) -> None:
        self.query_one("#errors", Static).update(f"✗ {text}" if text else "")

    @on(Button.Pressed, "#by-length")
    def _by_length(self) -> None:
        try:
            minutes = float(self.query_one("#minutes", Input).value)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            self._error("episode length must be a positive number of minutes")
            return
        self._split_by_length(minutes)
        self._error("")
        self._show_episodes()

    @on(Button.Pressed, "#by-count")
    def _by_count(self) -> None:
        n = _int_or_none(self.query_one("#fixed", Input).value)
        if not n or n < 1:
            self._error("chapters each must be 1 or more")
            return
        self.draft.episodes = fixed_split(self.draft.chapters, n, self._start_ep())  # type: ignore[arg-type]
        self._error("")
        self._show_episodes()

    @on(Button.Pressed, "#by-ranges")
    def _by_ranges(self) -> None:
        try:
            self.draft.episodes = parse_ranges(self.query_one("#ranges", Input).value,
                                               self.draft.chapters or [], self._start_ep())
        except ValueError as exc:
            self._error(str(exc))
            return
        self._error("")
        self._show_episodes()

    @on(Button.Pressed, "#next")
    def action_next(self) -> None:
        if self.draft.episodes:
            self.flow.goto("review")

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.flow.goto("tracks")


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class ReviewScreen(StepScreen):
    STEP = "Review"
    BINDINGS = [
        Binding("ctrl+s", "queue", "Queue jobs", priority=True),
        Binding("o", "override", "Settings for this file"),
        Binding("x", "skip", "Skip / include"),
        Binding("m", "skip_mismatched", "Skip mismatched"),
        Binding("n", "naming", "Naming"),
        Binding("l", "toggle_move", "Move to library"),
        Binding("escape", "back", "Back"),
    ]
    DEFAULT_CSS = """
    ReviewScreen #notes { margin-top: 1; }
    ReviewScreen #library { width: 1fr; }
    ReviewScreen #library-row.hidden { display: none; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="body"):
            yield Plain("", id="summary", classes="section")
            yield DataTable(cursor_type="row", zebra_stripes=True, id="jobs")
            yield Plain("", id="notes", classes="warn")
            yield Plain("Library", classes="section")
            yield Checkbox("Move to the library when encoded (l)", self.draft.moving,
                           id="move", compact=True)
            with Horizontal(classes="field", id="library-row"):
                yield Label("Library folder")
                yield Input(str(self.draft.library_dir or ""), id="library", compact=True,
                            placeholder="/path/to/Shows/Show (Year)")
            yield Plain("", id="library-note", classes="muted")
            yield Plain("", id="errors")
            with Horizontal(id="buttons"):
                yield Button("Back  esc", id="back", compact=True)
                yield Button("Queue  ctrl+s", variant="primary", id="queue", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Job", "Output", "Audio", "Subs", "Encode", "Notes")
        self._refresh()
        table.focus()

    def _refresh(self) -> None:
        d = self.draft
        plan_ = d.build_plan()
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        for i, job in enumerate(plan_):
            hb: HandbrakeArgs = job["handbrake_args"]
            notes = []
            if job["skipped"]:
                notes.append("skipped")
            if job["layout_warning"]:
                notes.append("⚠ different tracks")
            if job["override"]:
                notes.append("own settings")
            if job["errors"] and not job["skipped"]:
                notes.append("✗ error")
            encode = f"crf {hb.quality} {hb.preset}" + (f" {hb.tune}" if hb.tune else "")
            if hb.chapter_start is not None:
                encode += f" ch {hb.chapter_start}-{hb.chapter_end}"
            table.add_row(cell(job["label"]), cell(pathlib.Path(job["output_path"]).name),
                          _audio_summary(hb.audio_tracks),
                          _subtitle_summary(hb.subtitle_tracks) if hb.subtitle_tracks else "none",
                          encode, ", ".join(notes), key=str(i))
        if plan_:
            table.move_cursor(row=min(max(cursor, 0), len(plan_) - 1))
        count = sum(1 for j in plan_ if not j["skipped"])
        self.query_one("#summary", Static).update(
            f"{count} job(s) to queue → {d.staging_dir}")
        self.query_one("#library-row").set_class(not d.moving, "hidden")
        if d.moving and d.library_dir is not None:
            note = (f"Encodes move to {d.library_dir}/ when done; "
                    "raw rips stay in staging.")
        elif d.moving:
            note = "No [library] root for this kind in the config: type a folder."
        else:
            note = "Encodes stay in staging."
        self.query_one("#library-note", Static).update(note)
        self.query_one("#notes", Static).update("\n".join(d.mismatch_details(plan_)))
        errors = d.blocking_errors()
        self.query_one("#errors", Static).update(
            "\n".join(f"✗ {e}" for e in errors)
            + ("\nPress o to give a file its own settings, or x to skip it." if errors else ""))
        self.query_one("#queue", Button).disabled = bool(errors) or count == 0
        self._plan = plan_

    def _current_path(self) -> pathlib.Path | None:
        table = self.query_one(DataTable)
        if not self._plan or table.cursor_row < 0:
            return None
        return pathlib.Path(self._plan[table.cursor_row]["source_path"])

    def action_override(self) -> None:
        path = self._current_path()
        if path is None:
            return
        if path not in self.draft.summaries:
            self.notify("This file could not be probed; skip it instead.", severity="warning")
            return
        self.flow.goto("override", path=path)

    def action_skip(self) -> None:
        path = self._current_path()
        if path is not None:
            self.draft.skipped ^= {path}
            self._refresh()

    def action_skip_mismatched(self) -> None:
        for job in self._plan:
            if job["layout_warning"]:
                self.draft.skipped.add(pathlib.Path(job["source_path"]))
        self._refresh()

    def action_naming(self) -> None:
        self.flow.goto("naming")

    def action_toggle_move(self) -> None:
        box = self.query_one("#move", Checkbox)
        box.value = not box.value

    @on(Checkbox.Changed, "#move")
    def _move_changed(self, event: Checkbox.Changed) -> None:
        self.draft.move = event.value
        self._refresh()

    @on(Input.Changed, "#library")
    def _library_changed(self, event: Input.Changed) -> None:
        d = self.draft
        d.library_override = None  # back to the default to compare against it
        text = event.value.strip()
        if text and pathlib.Path(text).expanduser() != d.library_dir:
            d.library_override = pathlib.Path(text).expanduser()
        elif not text:
            d.library_override = pathlib.Path("")  # flagged as "not absolute"
        self._refresh()

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.flow.goto("chapters" if self.draft.naming.disc_split else "tracks")

    @on(Button.Pressed, "#queue")
    def action_queue(self) -> None:
        if self.query_one("#queue", Button).disabled:
            self.notify("Nothing to queue, or there are errors to fix.", severity="error")
            return
        self.flow.finish()


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


class PipelineApp(App[tuple[list[str], bool]]):
    """Returns (lines to print after the screen closes, success)."""

    CSS = APP_CSS
    TITLE = "rne"
    BINDINGS = [Binding("ctrl+q", "quit_flow", "Quit", priority=True)]

    def __init__(
        self,
        draft: Draft,
        *,
        conn: sqlite3.Connection,
        prober: Prober,
        chapter_prober: ChapterProber,
        titles: dict | None = None,
        display_order: list[int] | None = None,
        volume_name: str = "",
        ripper: Ripper | None = None,
        notifier: Notifier | None = None,
        auto_retries: int = 0,
        batch_notes: str = "",
        lookup: Lookup | None = None,
        lookup_note: str | None = None,
    ) -> None:
        super().__init__()
        self.draft = draft
        self.conn = conn
        self.prober = prober
        self.chapter_prober = chapter_prober
        self.titles = titles or {}
        self.display_order = display_order or []
        self.volume_name = volume_name
        self.ripper = ripper
        self.notifier = notifier
        self.auto_retries = auto_retries
        self.batch_notes = batch_notes
        self.lookup = lookup
        self.lookup_note = lookup_note
        self.ripping = False
        self.ripped = False  # ingest only: the rip has finished
        self.raw_dir: pathlib.Path | None = None

    def step_trail(self, current: str) -> str:
        steps = ["Titles", "Naming", "Rip", "Tracks"] if self.draft.mode == "ingest" \
            else ["Naming", "Tracks"]
        if self.draft.naming.disc_split:
            steps.append("Chapters")
        steps.append("Review")
        return "  ›  ".join(f"[{s}]" if s == current else s for s in steps)

    def on_mount(self) -> None:
        self.push_screen(self._make("titles" if self.draft.mode == "ingest" else "naming"))

    def title_line(self, disc_idx: int) -> str:
        """'#8 00224.mpls 0:30:07' — how the titles table showed this title."""
        info = self.titles.get(disc_idx, {}).get("info", {})
        display = self.display_order.index(disc_idx) if disc_idx in self.display_order else "?"
        parts = [f"#{display}", info.get(makemkv.T_SOURCE, ""), info.get(makemkv.T_DURATION, "")]
        return " ".join(p for p in parts if p)

    def _make(self, step: str, path: pathlib.Path | None = None) -> Screen:
        return {
            "titles": TitlesScreen,
            "naming": NamingScreen,
            "rip": RipScreen,
            "tracks": TracksScreen,
            "chapters": ChaptersScreen,
            "review": ReviewScreen,
        }[step]() if step != "override" else TracksScreen(override=path)

    def goto(self, step: str, *, path: pathlib.Path | None = None) -> None:
        self.switch_screen(self._make(step, path))

    # -- work shared by steps ----------------------------------------------------

    @work(exclusive=True, group="probe")
    async def probe_then(self, step: str) -> None:
        """Queue mode: read the files' tracks, then move on."""
        self.notify("Reading tracks ...", timeout=2)
        if await self.probe_sources():
            self.goto(step)

    async def probe_sources(self) -> bool:
        """Probe every source not probed yet. False (and exit) if the first fails."""
        d = self.draft
        for src in d.sources:
            if src.path in d.summaries:
                continue
            try:
                d.summaries[src.path] = await asyncio.to_thread(self.prober, src.path)
                d.probe_errors.pop(src.path, None)
            except Exception as exc:  # noqa: BLE001 - reported per file
                d.probe_errors[src.path] = str(exc)
        ref = d.reference
        if ref is not None and ref.path not in d.summaries:
            self.exit_with([f"Probe failed for {ref.path}: {d.probe_errors.get(ref.path)}"],
                           ok=False)
            return False
        return True

    def start_batch(self) -> pathlib.Path:
        """Ingest: create the batch row and its fresh raw folder."""
        d = self.draft
        n = d.naming
        d.batch_id = jobs.create_batch_row(
            self.conn, is_tv=n.is_tv, show=n.clean_title if n.is_tv else None,
            movie=None if n.is_tv else n.clean_title, season=n.season, notes=self.batch_notes,
            year=n.year, tvdb_id=n.tvdb_id,
        )
        self.raw_dir = d.staging_dir / "_raw" / f"batch-{d.batch_id}"
        self.raw_dir.mkdir(parents=True, exist_ok=False)
        return self.raw_dir

    def notify_rip_complete(self) -> str | None:
        if self.notifier is None:
            return None
        n = self.draft.naming
        return self.notifier(
            disc=self.volume_name, title=n.clean_title, kind="tv" if n.is_tv else "movie",
            season=n.season, count=len(self.draft.sources), batch=self.draft.batch_id,
        )

    def finish(self) -> None:
        d = self.draft
        n = d.naming
        to_queue = d.jobs_to_queue()
        show = n.clean_title if n.is_tv else None
        movie = None if n.is_tv else n.clean_title
        if d.batch_id is None:  # queue mode creates its batch only now
            d.batch_id = jobs.create_batch_row(self.conn, is_tv=n.is_tv, show=show, movie=movie,
                                               season=n.season, notes=self.batch_notes,
                                               label_suffix=" (queued)", year=n.year,
                                               tvdb_id=n.tvdb_id)
        else:  # naming may have changed since the rip started
            label = jobs.batch_label(is_tv=n.is_tv, show=show, movie=movie, season=n.season,
                                     year=n.year)
            self.conn.execute(
                "UPDATE ingest_batches SET label = ?, show = ?, movie = ?, year = ?, tvdb_id = ? "
                "WHERE id = ?", (label, show, movie, n.year, n.tvdb_id, d.batch_id))
        jobs.insert_jobs(self.conn, d.batch_id, to_queue)
        lines = [f"Queued {len(to_queue)} job(s) (batch {d.batch_id})."]
        if d.moving and d.library_dir is not None:
            lines.append(f"Encodes will be moved to {d.library_dir}/ when done.")
        if d.mode == "queue":
            lines.append("Source files left in place — do not move or delete them "
                         "until encoding completes.")
        else:
            lines.append("Worker will pick them up. Run `rne ls` to check status.")
        skipped = len(d.build_plan()) - len(to_queue)
        if skipped:
            lines.append(f"{skipped} skipped; their files were left untouched.")
        self.exit_with(lines, ok=True)

    def exit_with(self, lines: list[str], *, ok: bool) -> None:
        self.exit((lines, ok))

    def action_quit_flow(self) -> None:
        if self.ripping:
            self.notify("The rip is still running; wait for it, or abort from the "
                        "failure prompt.", severity="warning")
            return
        lines = ["Quit; nothing was queued."]
        if self.raw_dir is not None:
            lines.append(f"Raw files are in {self.raw_dir}/ — queue them later with "
                         f"`rne queue {self.raw_dir}`.")
        self.exit_with(lines, ok=True)
