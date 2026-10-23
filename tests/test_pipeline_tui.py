"""Tests for the full-screen ingest/queue flow: Draft, rip progress, and the app."""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from textual.widgets import Checkbox, DataTable, Input

from rne import makemkv, probe
from rne.cli.disc_split import parse_ranges
from rne.models import AudioTrack, HandbrakeArgs
from rne.probe import Chapter, StreamSummary
from rne.tui.flow import Draft, Naming, Source
from rne.tui.pipeline import (
    ChaptersScreen,
    NamingScreen,
    PipelineApp,
    ReviewScreen,
    RipFailedModal,
    RipScreen,
    TitlesScreen,
    TracksScreen,
)
from tests.test_jobs import SUMMARY  # truehd 5.1 + ac3 2.0, one subtitle

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "makemkv_info_sample.txt"
ONE_AUDIO = StreamSummary(video=SUMMARY.video, audio=SUMMARY.audio[:1], subtitle=[])
CHAPTERS = [Chapter(i, (i - 1) * 600.0, i * 600.0, f"Chapter {i:02d}") for i in range(1, 7)]


# ---------------------------------------------------------------------------
# Draft (pure)
# ---------------------------------------------------------------------------


def _draft(mode="queue", n=3, **naming) -> Draft:
    base = dict(is_tv=True, title="Show", season=1, first_episode=5)
    base.update(naming)
    d = Draft(mode=mode, naming=Naming(**base), staging_root=pathlib.Path("/staging"))
    d.sources = [Source(pathlib.Path(f"/r/{i}.mkv"), title_idx=10 + i) for i in range(n)]
    d.summaries = {s.path: SUMMARY for s in d.sources}
    return d


def test_naming_errors():
    assert Naming(title="  ", season=None, first_episode=0).errors() == [
        "name can't be empty",
        "season must be 0 (specials) or more",
        "first episode must be 1 or more",
    ]
    assert Naming(is_tv=False, title="Heat").errors() == []
    assert Naming(title="A: B?").clean_title == "A B"


def test_episode_numbers_keep_gaps_for_skipped_titles():
    d = _draft(mode="ingest", n=0)
    d.selected_titles = [3, 1, 7]
    d.sources = [Source(pathlib.Path("/r/a.mkv"), 3), Source(pathlib.Path("/r/c.mkv"), 7)]
    assert d.episode_numbers() == [5, 7]
    assert d.episode_preview() == "S01E05, S01E07"
    assert d.episode_preview(count=3) == "S01E05, S01E06, S01E07"


def test_build_plan_tv_uses_defaults_and_staging_dir():
    d = _draft()
    plan_ = d.build_plan()
    assert [j["label"] for j in plan_] == ["S01E05", "S01E06", "S01E07"]
    assert plan_[0]["output_path"] == "/staging/Show/Season 01/Show - S01E05.mkv"
    assert plan_[0]["handbrake_args"].audio_tracks == [AudioTrack(1, "ac3", 640)]
    assert all(j["source_summary"] is SUMMARY and not j["layout_warning"] for j in plan_)
    d.staging_override = pathlib.Path("/elsewhere")
    assert d.build_plan()[0]["output_path"] == "/elsewhere/Season 01/Show - S01E05.mkv"


@pytest.mark.parametrize("mode,suffixes", [("ingest", ["_t10", "_t11"]), ("queue", ["_00", "_01"])])
def test_build_plan_movie_suffixes(mode, suffixes):
    d = _draft(mode=mode, n=2, is_tv=False, title="Heat")
    assert [pathlib.Path(j["output_path"]).name for j in d.build_plan()] == [
        f"Heat{s}.mkv" for s in suffixes
    ]
    one = _draft(mode=mode, n=1, is_tv=False, title="Heat")
    assert one.build_plan()[0]["output_path"] == "/staging/Heat/Heat.mkv"


def test_build_plan_override_skip_mismatch_and_errors():
    d = _draft()
    third = d.sources[2].path
    d.summaries[third] = ONE_AUDIO
    d.template = HandbrakeArgs(audio_tracks=[AudioTrack(2)])
    plan_ = d.build_plan()
    assert plan_[2]["layout_warning"]
    assert plan_[2]["errors"] == ["audio track 2 does not exist (source has 1)"]
    assert d.blocking_errors() == ["S01E07: audio track 2 does not exist (source has 1)"]
    assert d.mismatch_details(plan_) == [
        "S01E07 differs: audio track count 1 vs reference 2; subtitle track count 0 vs reference 1."
    ]

    d.overrides[third] = HandbrakeArgs(audio_tracks=[AudioTrack(1)])
    assert d.blocking_errors() == []
    assert d.build_plan()[2]["override"]

    d.skipped.add(d.sources[0].path)
    assert [j["label"] for j in d.jobs_to_queue()] == ["S01E06", "S01E07"]


def test_build_plan_unprobed_file_is_flagged():
    d = _draft()
    path = d.sources[1].path
    del d.summaries[path]
    d.probe_errors[path] = "boom"
    job = d.build_plan()[1]
    assert job["layout_warning"] and job["errors"] == ["could not probe: boom"]


def test_build_plan_disc_split():
    d = _draft(n=1, disc_split=True)
    d.chapters = CHAPTERS
    d.episodes = parse_ranges("1-3, 4-6", CHAPTERS, 5)
    plan_ = d.build_plan()
    assert [(j["label"], j["handbrake_args"].chapter_start, j["handbrake_args"].chapter_end)
            for j in plan_] == [("S01E05", 1, 3), ("S01E06", 4, 6)]
    assert plan_[0]["source_chapters"] is CHAPTERS


def test_build_plan_duplicate_outputs():
    d = _draft(n=2, is_tv=False, title="Heat")
    d.sources[1] = Source(d.sources[0].path, 11)  # same file twice → same plan entry name
    d.mode = "ingest"
    d.sources[1].title_idx = 10
    assert any("used by more than one job" in e for e in d.blocking_errors())


# ---------------------------------------------------------------------------
# Chapter ranges
# ---------------------------------------------------------------------------


def test_parse_ranges():
    eps = parse_ranges("1-2 3,4-6", CHAPTERS, 3)
    assert [(e.number, e.chapter_start, e.chapter_end) for e in eps] == [
        (3, 1, 2), (4, 3, 3), (5, 4, 6)
    ]


@pytest.mark.parametrize("text", ["", "0-2", "1-7", "3-1", "1-3, 2-4", "a-b", "4, 2"])
def test_parse_ranges_rejects(text):
    with pytest.raises(ValueError):
        parse_ranges(text, CHAPTERS, 1)


# ---------------------------------------------------------------------------
# Rip progress
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line,event", [
    ("PRGV:100,32768,65536", makemkv.RipEvent("progress", fraction=0.5)),
    ('PRGC:5018,0,"Saving to MKV file"', makemkv.RipEvent("task", text="Saving to MKV file")),
    ('MSG:5003,0,0,"Saving 1 titles into directory","%1",""',
     makemkv.RipEvent("message", text="Saving 1 titles into directory")),
    ("DRV:0,2,999,1,\"BD\",\"X\",\"/dev/sr0\"", None),
    ("garbage", None),
])
def test_parse_rip_line(line, event):
    assert makemkv.parse_rip_line(line) == event


def test_rip_title_reports_events_and_finds_file(tmp_path):
    events = []

    def fake_run(cmd):
        assert cmd[:3] == ["makemkvcon", "-r", "--progress=-same"]
        assert cmd[-3:] == ["disc:0", "4", str(tmp_path)]
        (tmp_path / "B1_t04.mkv").touch()
        return ["PRGV:0,65536,65536\n", 'MSG:1,0,0,"done","",""\n'], lambda: 0

    out = makemkv.rip_title("disc:0", 4, tmp_path, 900, events.append, run=fake_run)
    assert out == tmp_path / "B1_t04.mkv"
    assert [e.kind for e in events] == ["progress", "message"]


def test_rip_title_failure_carries_messages(tmp_path):
    def fake_run(cmd):
        return ['MSG:5085,0,0,"Evaluation period has expired.","",""\n'], lambda: 1

    with pytest.raises(makemkv.RipFailed) as exc:
        makemkv.rip_title("disc:0", 1, tmp_path, 900, lambda e: None, run=fake_run)
    assert exc.value.messages == ["Evaluation period has expired."]
    assert "Evaluation period has expired." in str(exc.value)


# ---------------------------------------------------------------------------
# The app, headless
# ---------------------------------------------------------------------------


def _run(app, steps):
    async def main():
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            await steps(pilot, app)
            await pilot.pause()
        return app.return_value
    return asyncio.run(main())


async def _settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


class FakeDisc:
    """Ripper that writes a file per title; fails the titles in `fail`."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def __call__(self, title_idx, raw_dir, on_event):
        self.calls.append(title_idx)
        on_event(makemkv.RipEvent("task", text="Saving to MKV file"))
        on_event(makemkv.RipEvent("progress", fraction=1.0))
        if title_idx in self.fail:
            raise makemkv.RipFailed(title_idx, 1, ["read error"])
        path = raw_dir / f"title_{title_idx:02d}.mkv"
        path.touch()
        return path


def _ingest_app(conn, tmp_path, ripper, **naming):
    disc_info, titles = makemkv.parse_info(FIXTURE.read_text())
    from rne.cli.ingest import _build_display_order

    notes = []
    draft = Draft(mode="ingest", naming=Naming(title="EXAMPLE_DISC", **naming),
                  staging_root=tmp_path / "staging")
    app = PipelineApp(
        draft, conn=conn, prober=lambda p: SUMMARY, chapter_prober=lambda p: CHAPTERS,
        titles=titles, display_order=_build_display_order(titles), volume_name="EXAMPLE_DISC",
        ripper=ripper, notifier=lambda **v: notes.append(v) or "Notification sent: test",
        batch_notes="EXAMPLE_DISC",
    )
    return app, notes


def _fill_naming(screen, title, season, first):
    screen.query_one("#title", Input).value = title
    screen.query_one("#season", Input).value = str(season)
    screen.query_one("#first", Input).value = str(first)


def test_ingest_happy_path(conn, tmp_path):
    ripper = FakeDisc()
    app, notes = _ingest_app(conn, tmp_path, ripper)

    async def steps(pilot, app):
        assert isinstance(app.screen, TitlesScreen)
        await pilot.press("enter")  # nothing selected yet
        assert "select at least one title" in str(app.screen.query_one("#errors").render())
        # Display order: 0, 5, 6, ... → select display rows 2 and 3 (disc 6, 7).
        await pilot.press("down", "down", "space", "down", "space", "enter")
        await pilot.pause()
        assert isinstance(app.screen, NamingScreen)
        _fill_naming(app.screen, "Initial D", 1, 5)
        await pilot.pause()
        assert "S01E05, S01E06" in str(app.screen.query_one("#preview").render())
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, RipScreen)
        await pilot.click("#start")
        await _settle(pilot)
        assert isinstance(app.screen, TracksScreen)
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert app.screen.query_one(DataTable).row_count == 2
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    assert ok and lines[0] == "Queued 2 job(s) (batch 1)."
    assert ripper.calls == [6, 7]
    raw = tmp_path / "staging" / "Initial D" / "_raw" / "batch-1"
    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert [(r["episode"], r["source_path"]) for r in rows] == [
        (5, str(raw / "title_06.mkv")), (6, str(raw / "title_07.mkv"))
    ]
    assert rows[0]["output_path"].endswith("Initial D/Season 01/Initial D - S01E05.mkv")
    assert probe.snapshot_from_json(rows[0]["source_probe"])[0] == SUMMARY
    assert HandbrakeArgs.from_json(rows[0]["handbrake_args"]).audio_tracks == [
        AudioTrack(1, "ac3", 640)
    ]
    assert notes == [{"disc": "EXAMPLE_DISC", "title": "Initial D", "kind": "tv", "season": 1,
                      "count": 2, "batch": 1}]
    batch = conn.execute("SELECT label, notes FROM ingest_batches").fetchone()
    assert (batch["label"], batch["notes"]) == ("Initial D S01", "EXAMPLE_DISC")


def test_ingest_skip_failed_title_keeps_episode_numbers(conn, tmp_path):
    ripper = FakeDisc(fail={6})
    app, _ = _ingest_app(conn, tmp_path, ripper, season=2, first_episode=1)

    async def steps(pilot, app):
        await pilot.press("down", "space", "down", "space", "down", "space", "enter")
        await pilot.pause()
        app.screen.query_one("#title", Input).value = "Show"
        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, RipFailedModal):
                break
        assert isinstance(app.screen, RipFailedModal)
        await pilot.press("s")
        await _settle(pilot)
        assert isinstance(app.screen, TracksScreen)
        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    assert ok
    assert [r["episode"] for r in conn.execute("SELECT episode FROM jobs ORDER BY id")] == [1, 3]


def test_ingest_rename_after_rip_moves_outputs_not_raws(conn, tmp_path):
    app, _ = _ingest_app(conn, tmp_path, FakeDisc(), season=1, first_episode=1)

    async def steps(pilot, app):
        await pilot.press("down", "space", "enter")
        await pilot.pause()
        app.screen.query_one("#title", Input).value = "Wrong"
        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.click("#start")
        await _settle(pilot)
        await pilot.press("ctrl+n")  # tracks → review
        await pilot.pause()
        await pilot.press("n")  # back to naming
        await pilot.pause()
        naming = app.screen
        assert isinstance(naming, NamingScreen)
        assert naming.query_one("#kind").disabled  # kind is fixed after the rip
        naming.query_one("#title", Input).value = "Right"
        await pilot.press("ctrl+n")  # → tracks
        await pilot.pause()
        await pilot.press("ctrl+n")  # → review
        await pilot.pause()
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    row = conn.execute("SELECT show, source_path, output_path FROM jobs").fetchone()
    assert row["show"] == "Right"
    assert "/Wrong/_raw/batch-1/" in row["source_path"]
    assert row["output_path"].endswith("/Right/Season 01/Right - S01E01.mkv")
    assert conn.execute("SELECT label FROM ingest_batches").fetchone()[0] == "Right S01"


def test_titles_back_navigation_keeps_values(conn, tmp_path):
    app, _ = _ingest_app(conn, tmp_path, FakeDisc())

    async def steps(pilot, app):
        await pilot.press("space", "enter")
        await pilot.pause()
        _fill_naming(app.screen, "Kept", 3, 9)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, TitlesScreen)
        assert app.draft.selected_titles == [0]
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#title", Input).value == "Kept"
        assert app.screen.query_one("#first", Input).value == "9"
        await pilot.press("ctrl+q")

    assert _run(app, steps) == (["Quit; nothing was queued."], True)
    assert conn.execute("SELECT COUNT(*) FROM ingest_batches").fetchone()[0] == 0


def _queue_app(conn, tmp_path, files, summaries):
    paths = []
    for name in files:
        p = tmp_path / name
        p.touch()
        paths.append(p)
    draft = Draft(mode="queue", naming=Naming(title="hint"), staging_root=tmp_path / "staging",
                  sources=[Source(p) for p in paths])
    app = PipelineApp(draft, conn=conn, prober=lambda p: summaries[p.name],
                      chapter_prober=lambda p: CHAPTERS, batch_notes="Queued from test")
    return app


def test_queue_disc_split(conn, tmp_path):
    app = _queue_app(conn, tmp_path, ["disc.mkv"], {"disc.mkv": SUMMARY})

    async def steps(pilot, app):
        screen = app.screen
        assert isinstance(screen, NamingScreen)
        _fill_naming(screen, "Anime", 1, 1)
        screen.query_one("#split", Checkbox).value = True
        await pilot.pause()
        await pilot.press("ctrl+n")
        await _settle(pilot)
        assert isinstance(app.screen, TracksScreen)
        await pilot.press("ctrl+n")
        await _settle(pilot)
        assert isinstance(app.screen, ChaptersScreen)
        app.screen.query_one("#fixed", Input).value = "2"
        await pilot.click("#by-count")
        await pilot.pause()
        assert app.screen.query_one("#episodes", DataTable).row_count == 3
        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    assert ok and "Source files left in place" in lines[1]
    rows = conn.execute("SELECT episode, handbrake_args FROM jobs ORDER BY id").fetchall()
    assert [(r["episode"], HandbrakeArgs.from_json(r["handbrake_args"]).chapter_start)
            for r in rows] == [(1, 1), (2, 3), (3, 5)]
    batch = conn.execute("SELECT label, notes FROM ingest_batches").fetchone()
    assert (batch["label"], batch["notes"]) == ("Anime S01 (queued)", "Queued from test")


def test_queue_review_errors_override_and_skip(conn, tmp_path):
    app = _queue_app(conn, tmp_path, ["a.mkv", "b.mkv", "c.mkv"],
                     {"a.mkv": SUMMARY, "b.mkv": SUMMARY, "c.mkv": ONE_AUDIO})

    async def steps(pilot, app):
        _fill_naming(app.screen, "Show", 1, 1)
        await pilot.press("ctrl+n")
        await _settle(pilot)
        # Keep only audio track 2 (missing from c.mkv).
        from rne.tui.settings import AudioRowWidget
        rows = list(app.screen.query(AudioRowWidget))
        rows[0].query_one(".include", Checkbox).value = False
        rows[1].query_one(".include", Checkbox).value = True
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        review = app.screen
        assert review.query_one("#queue").disabled
        assert "audio track 2 does not exist" in str(review.query_one("#errors").render())
        # Give c.mkv its own settings: the third row.
        await pilot.press("down", "down", "o")
        await pilot.pause()
        assert isinstance(app.screen, TracksScreen) and app.screen.override.name == "c.mkv"
        await pilot.press("ctrl+n")  # defaults for c.mkv: its only track
        await pilot.pause()
        assert not app.screen.query_one("#queue").disabled
        await pilot.press("up", "up", "x")  # skip a.mkv
        await pilot.pause()
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    assert ok and lines[0] == "Queued 2 job(s) (batch 1)."
    assert "1 skipped" in lines[-1]
    rows = conn.execute("SELECT source_path, handbrake_args FROM jobs ORDER BY id").fetchall()
    assert [pathlib.Path(r["source_path"]).name for r in rows] == ["b.mkv", "c.mkv"]
    assert HandbrakeArgs.from_json(rows[0]["handbrake_args"]).audio_tracks == [AudioTrack(2)]
    assert HandbrakeArgs.from_json(rows[1]["handbrake_args"]).audio_tracks == [
        AudioTrack(1, "ac3", 640)
    ]


def test_queue_first_probe_failure_exits(conn, tmp_path):
    def broken(p):
        raise RuntimeError("not a video")

    app = _queue_app(conn, tmp_path, ["a.mkv"], {})
    app.prober = broken

    async def steps(pilot, app):
        _fill_naming(app.screen, "Show", 1, 1)
        await pilot.press("ctrl+n")
        await _settle(pilot)

    lines, ok = _run(app, steps)
    assert not ok and "not a video" in lines[0]




@pytest.mark.parametrize("argv,plain", [
    (["ingest"], False), (["ingest", "--plain"], True),
    (["queue", "x.mkv"], False), (["queue", "--plain", "x.mkv"], True),
])
def test_parser_plain_flag(argv, plain):
    from rne.cli import _build_parser

    assert _build_parser().parse_args(argv).plain is plain
