"""Tests for editing jobs: track rows, renaming, the edit model, and the edit TUI."""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from textual.widgets import Checkbox, DataTable, Input, Select

from rne import jobs, naming, plan, probe
from rne.cli import _build_parser
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack
from rne.probe import StreamSummary, SubtitleStream
from rne.tui.edit import EditApp, EditScreen, PickerScreen
from rne.tui.settings import AudioRowWidget, SettingsForm, SubtitleRowWidget
from tests.conftest import insert_job
from tests.test_jobs import SUMMARY  # truehd 5.1 + ac3 2.0, one PGS subtitle

TWO_SUBS = StreamSummary(
    video=SUMMARY.video,
    audio=SUMMARY.audio,
    subtitle=SUMMARY.subtitle + [SubtitleStream("hdmv_pgs_subtitle", "eng", "", False, False, 20)],
)


# ---------------------------------------------------------------------------
# plan: track rows
# ---------------------------------------------------------------------------


def test_audio_rows_chosen_first_then_rest_with_recommendation():
    args = HandbrakeArgs(audio_tracks=[AudioTrack(2)])
    rows = plan.audio_rows(SUMMARY, args)
    assert [(r.track, r.include, r.codec, r.bitrate) for r in rows] == [
        (2, True, "copy", None),
        (1, False, "ac3", 640),  # truehd 5.1 → recommended AC3 640
    ]
    assert rows[0].label.startswith("2: ac3 2.0 eng")


def test_audio_rows_track_missing_from_source():
    rows = plan.audio_rows(SUMMARY, HandbrakeArgs(audio_tracks=[AudioTrack(5)]))
    assert rows[0].label == "5: not in source file" and rows[0].include


def test_subtitle_rows():
    args = HandbrakeArgs(subtitle_tracks=[SubtitleTrack(2, default=True), SubtitleTrack(4)])
    rows = plan.subtitle_rows(TWO_SUBS, args)
    assert [(r.track, r.include, r.default) for r in rows] == [
        (1, False, False), (2, True, True), (4, True, False),
    ]
    assert "(likely forced)" in rows[1].label


def test_args_from_rows_keeps_job_specifics():
    base = HandbrakeArgs(encoder="x265_10b", extra_args=["--x"], chapter_start=3, chapter_end=5)
    audio = plan.audio_rows(SUMMARY, base)  # track 1 included as copy
    audio[0].codec, audio[0].bitrate = "ac3", None  # transcode without a pick → recommended
    audio[1].include = True
    subs = plan.subtitle_rows(SUMMARY, base)
    subs[0].include = subs[0].default = True
    out = plan.args_from_rows(base, audio, subs, quality=18, preset="medium",
                              tune="animation", detelecine=True, decomb=False)
    assert out == HandbrakeArgs(
        encoder="x265_10b", quality=18, preset="medium",
        audio_tracks=[AudioTrack(1, "ac3", 640), AudioTrack(2, "copy")],
        subtitle_tracks=[SubtitleTrack(1, True)], detelecine=True, decomb=False,
        tune="animation", extra_args=["--x"], chapter_start=3, chapter_end=5,
    )


# ---------------------------------------------------------------------------
# naming.renamed_output
# ---------------------------------------------------------------------------


def test_renamed_output_tv_renames_show_folder():
    out = naming.renamed_output("/s/Initial D/Season 01/Initial D - S01E05.mkv", is_tv=True,
                                old_title="Initial D", new_title="Initial D First Stage",
                                season=1, episode=6)
    assert out == pathlib.Path(
        "/s/Initial D First Stage/Season 01/Initial D First Stage - S01E06.mkv"
    )


def test_renamed_output_tv_keeps_custom_folder():
    out = naming.renamed_output("/custom/Season 01/X - S01E01.mkv", is_tv=True,
                                old_title="X", new_title="Y", season=2, episode=1)
    assert out == pathlib.Path("/custom/Season 02/Y - S02E01.mkv")


def test_renamed_output_movie_keeps_suffix():
    out = naming.renamed_output("/s/Heat/Heat_t03.mkv", is_tv=False,
                                old_title="Heat", new_title="Heat 1995")
    assert out == pathlib.Path("/s/Heat 1995/Heat 1995_t03.mkv")


def test_renamed_output_tv_needs_numbers():
    with pytest.raises(ValueError):
        naming.renamed_output("/a/b/c.mkv", is_tv=True, old_title="a", new_title="a")


# ---------------------------------------------------------------------------
# jobs: edit model
# ---------------------------------------------------------------------------


def _tv_job(conn, root, ep, *, status="queued", batch=None, args=None, snapshot=True) -> int:
    job_id = insert_job(
        conn, show="Initial D", season=1, episode=ep, status=status,
        source_path=f"/r/t{ep}.mkv",
        output_path=str(root / "Initial D" / "Season 01" / f"Initial D - S01E{ep:02d}.mkv"),
        handbrake_args=(args or HandbrakeArgs()).to_json(),
    )
    conn.execute(
        "UPDATE jobs SET ingest_batch_id = ?, source_probe = ? WHERE id = ?",
        (batch, probe.snapshot_to_json(SUMMARY) if snapshot else None, job_id),
    )
    conn.commit()
    return job_id


@pytest.fixture
def batch(conn, tmp_path):
    batch_id = jobs.create_batch_row(conn, is_tv=True, show="Initial D", movie=None,
                                     season=1, notes="")
    ids = [
        _tv_job(conn, tmp_path, ep, batch=batch_id,
                args=HandbrakeArgs(chapter_start=ep, chapter_end=ep) if ep == 6 else None)
        for ep in (5, 6, 7)
    ]
    return batch_id, ids


def test_batch_job_ids_skips_running(conn, tmp_path, batch):
    batch_id, ids = batch
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (ids[1],))
    assert jobs.batch_job_ids(conn, batch_id) == [ids[0], ids[2]]


def test_load_edit_targets_unreadable_source(conn, tmp_path):
    job_id = _tv_job(conn, tmp_path, 1, snapshot=False)

    def broken(path):
        raise FileNotFoundError(path)

    (t,) = jobs.load_edit_targets(conn, [job_id], probe_file=broken)
    assert t.summary is None and "cannot read /r/t1.mkv" in t.probe_error
    assert (t.title, t.label, t.is_tv) == ("Initial D", "S01E01", True)


def test_plan_edits_renumbers_and_keeps_chapters(conn, tmp_path, batch):
    _, ids = batch
    targets = jobs.load_edit_targets(conn, ids)
    template = HandbrakeArgs(quality=16, audio_tracks=[AudioTrack(2)])
    results = jobs.plan_edits(targets, template=template, title="Initial D", first_episode=1)
    assert [r.label for r in results] == ["S01E01", "S01E02", "S01E03"]
    assert results[0].output_path.endswith("Season 01/Initial D - S01E01.mkv")
    assert all(r.args.quality == 16 for r in results)
    assert (results[1].args.chapter_start, results[1].args.chapter_end) == (6, 6)
    assert results[0].args.chapter_start is None
    assert all(r.errors == [] for r in results)


def test_plan_edits_without_template_keeps_settings(conn, tmp_path, batch):
    _, ids = batch
    targets = jobs.load_edit_targets(conn, ids)
    results = jobs.plan_edits(targets, template=None, title="X")
    assert [r.args for r in results] == [t.args for t in targets]
    assert [r.episode for r in results] == [5, 6, 7]


def test_plan_edits_reports_errors(conn, tmp_path, batch):
    _, ids = batch
    targets = jobs.load_edit_targets(conn, ids[:2])
    # Same episode for both → same output; track 9 doesn't exist.
    targets[1].episode = 5
    results = jobs.plan_edits(targets, template=HandbrakeArgs(audio_tracks=[AudioTrack(9)]),
                              title="Initial D")
    errors = [e for r in results for e in r.errors]
    assert "S01E05: audio track 9 does not exist (source has 2)" in errors
    assert any("is used by more than one job" in e for e in errors)


def test_apply_edits_saves_and_reports_running(conn, tmp_path, batch):
    _, ids = batch
    results = jobs.plan_edits(jobs.load_edit_targets(conn, ids), template=None,
                              title="Initial D", first_episode=10)
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (ids[2],))
    lines, ok = jobs.apply_edits(conn, results)
    assert not ok
    assert lines[0] == f"Job {ids[0]} (S01E10) updated."
    assert lines[2] == f"Job {ids[2]} (S01E12) is now running; not changed."
    row = conn.execute("SELECT episode, output_path FROM jobs WHERE id = ?", (ids[0],)).fetchone()
    assert row["episode"] == 10 and row["output_path"].endswith("S01E10.mkv")
    assert (tmp_path / "Initial D" / "Season 01").is_dir()


def test_update_job_missing(conn):
    with pytest.raises(jobs.JobNotFound):
        jobs.update_job(conn, 5, HandbrakeArgs(), show=None, season=None, episode=None,
                        movie="M", output_path="/tmp/m.mkv")


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv,expected", [
    (["edit"], (None, None, False)),
    (["edit", "4"], (4, None, False)),
    (["edit", "--batch", "9"], (None, 9, False)),
    (["edit", "-b", "9"], (None, 9, False)),
    (["edit", "4", "--raw"], (4, None, True)),
])
def test_parser_edit_forms(argv, expected):
    a = _build_parser().parse_args(argv)
    assert (a.id, a.batch, a.raw) == expected


def test_parser_edit_id_and_batch_conflict():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["edit", "4", "--batch", "9"])


# ---------------------------------------------------------------------------
# TUI (headless, via Textual's pilot)
# ---------------------------------------------------------------------------


def run_app(app, steps):
    """Run app headless, await steps(pilot, app), and return the app's result."""
    async def main():
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            await steps(pilot, app)
            await pilot.pause()
        return app.return_value
    return asyncio.run(main())


def test_tui_edit_single_job_saves_settings_and_episode(conn, tmp_path):
    job_id = _tv_job(conn, tmp_path, 5)

    async def steps(pilot, app):
        screen = app.screen
        assert isinstance(screen, EditScreen)
        screen.query_one("#quality", Select).value = 17
        screen.query_one("#preset", Select).value = "medium"
        rows = list(screen.query(AudioRowWidget))
        rows[1].query_one(".include", Checkbox).value = True  # add track 2 (ac3 copy)
        rows[1].move(-1)  # ...and make it the default
        screen.query_one("#episode", Input).value = "8"
        await pilot.pause()
        assert "S01E08" in str(screen.query_one("#outputs").render())
        await pilot.press("ctrl+s")

    lines, ok = run_app(EditApp(conn, [job_id]), steps)
    assert ok and lines == [f"Job {job_id} (S01E08) updated."]
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    args = HandbrakeArgs.from_json(row["handbrake_args"])
    assert (args.quality, args.preset) == (17, "medium")
    assert args.audio_tracks == [AudioTrack(2, "copy"), AudioTrack(1, "copy")]
    assert row["episode"] == 8 and row["output_path"].endswith("Initial D - S01E08.mkv")


def test_tui_subtitle_default_is_exclusive(conn, tmp_path):
    job_id = _tv_job(conn, tmp_path, 5)
    conn.execute("UPDATE jobs SET source_probe = ?", (probe.snapshot_to_json(TWO_SUBS),))
    conn.commit()

    async def steps(pilot, app):
        subs = list(app.screen.query(SubtitleRowWidget))
        subs[0].query_one(".default", Checkbox).value = True
        await pilot.pause()
        subs[1].query_one(".default", Checkbox).value = True
        await pilot.pause()
        assert [s.read().default for s in subs] == [False, True]
        assert [s.read().include for s in subs] == [True, True]  # default implies include
        await pilot.press("ctrl+s")

    run_app(EditApp(conn, [job_id]), steps)
    args = HandbrakeArgs.from_json(conn.execute("SELECT handbrake_args FROM jobs").fetchone()[0])
    assert args.subtitle_tracks == [SubtitleTrack(1), SubtitleTrack(2, True)]


def test_tui_invalid_input_blocks_save(conn, tmp_path):
    job_id = _tv_job(conn, tmp_path, 5)

    async def steps(pilot, app):
        screen = app.screen
        screen.query_one("#title", Input).value = ""
        for row in screen.query(AudioRowWidget):
            row.query_one(".include", Checkbox).value = False
        await pilot.pause()
        errors = str(screen.query_one("#errors").render())
        assert "name can't be empty" in errors
        assert screen.query_one("#save").disabled
        await pilot.press("ctrl+s")  # refused
        assert isinstance(app.screen, EditScreen)
        await pilot.press("escape")

    assert run_app(EditApp(conn, [job_id]), steps) == (["Edit cancelled; nothing changed."], True)


def test_tui_batch_edit_renumbers(conn, tmp_path, batch):
    _, ids = batch

    async def steps(pilot, app):
        screen = app.screen
        assert "Batch of 3 jobs" in str(screen.query_one("#heading").render())
        assert screen.query_one("#episode", Input).value == "5"
        screen.query_one("#episode", Input).value = "1"
        screen.query_one(SettingsForm).query_one("#tune", Select).value = "animation"
        await pilot.pause()
        await pilot.press("ctrl+s")

    lines, ok = run_app(EditApp(conn, ids), steps)
    assert ok and len(lines) == 3
    rows = conn.execute("SELECT episode, handbrake_args FROM jobs ORDER BY id").fetchall()
    assert [r["episode"] for r in rows] == [1, 2, 3]
    assert all(HandbrakeArgs.from_json(r["handbrake_args"]).tune == "animation" for r in rows)
    assert HandbrakeArgs.from_json(rows[1]["handbrake_args"]).chapter_start == 6


def test_tui_running_job_refused(conn, tmp_path):
    job_id = _tv_job(conn, tmp_path, 5, status="running")

    async def steps(pilot, app):
        pass

    lines, ok = run_app(EditApp(conn, [job_id]), steps)
    assert not ok and "currently running" in lines[0]


def test_tui_missing_source_allows_naming_only(conn, tmp_path, monkeypatch):
    job_id = _tv_job(conn, tmp_path, 5, snapshot=False)
    def unreadable(conn, job_id, **kwargs):
        raise FileNotFoundError("/r/t5.mkv")

    monkeypatch.setattr(jobs, "load_source_summary", unreadable)

    async def steps(pilot, app):
        screen = app.screen
        assert not screen.query(SettingsForm)
        screen.query_one("#title", Input).value = "Initial D Stage 1"
        await pilot.pause()
        await pilot.press("ctrl+s")

    lines, ok = run_app(EditApp(conn, [job_id]), steps)
    assert ok
    row = conn.execute("SELECT show, handbrake_args FROM jobs").fetchone()
    assert row["show"] == "Initial D Stage 1"
    assert HandbrakeArgs.from_json(row["handbrake_args"]) == HandbrakeArgs()


def test_tui_picker_opens_job_and_batch(conn, tmp_path, batch):
    batch_id, ids = batch
    insert_job(conn, movie="Heat", status="running")  # not listed

    async def steps(pilot, app):
        picker = app.screen
        assert isinstance(picker, PickerScreen)
        table = picker.query_one(DataTable)
        assert table.row_count == 3
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EditScreen) and len(app.screen.targets) == 1
        await pilot.press("escape")  # back to the picker
        await pilot.pause()
        assert app.screen is picker
        await pilot.press("b")
        await pilot.pause()
        assert len(app.screen.targets) == 3
        await pilot.press("ctrl+s")

    lines, ok = run_app(EditApp(conn, None), steps)
    assert ok and len(lines) == 3
