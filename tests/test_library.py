"""Tests for moving finished encodes into the library (Phase 3)."""

from __future__ import annotations

import asyncio
import errno
import os
import pathlib
import types
from unittest.mock import patch

import pytest
from textual.widgets import Checkbox, Input

from rne import config, db, jobs, naming, probe
from rne.config import LibraryConfig
from rne.models import AudioTrack, HandbrakeArgs, Job
from rne.tui.edit import EditApp
from rne.tui.flow import Draft, Naming, Source
from rne.tui.pipeline import PipelineApp, ReviewScreen, TracksScreen
from rne.worker import mover
from rne.worker.runner import run_job
from tests.conftest import insert_job
from tests.test_jobs import SUMMARY

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _cfg(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_library_config(tmp_path):
    assert config.load_library_config(tmp_path / "none.toml") == LibraryConfig()
    lib = config.load_library_config(_cfg(tmp_path, '[library]\ntv_root = "/m/Shows"\n'))
    assert lib == LibraryConfig(tv_root=pathlib.Path("/m/Shows"))
    assert lib.root_for(True) == pathlib.Path("/m/Shows") and lib.root_for(False) is None


@pytest.mark.parametrize("body", [
    '[library]\ntv_root = "Shows"\n',
    '[library]\ntv_root = ""\n',
    '[library]\nmovie_root = "/m"\n',
    'library = "/m"\n',
])
def test_library_config_malformed(tmp_path, body):
    with pytest.raises(config.ConfigError):
        config.load_library_config(_cfg(tmp_path, body))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_library_path_mirrors_staging_layout():
    lib = pathlib.Path("/m/Shows/Show (2025)")
    assert naming.library_path(lib, "/s/Show (2025)/Season 01/Show - S01E01.mkv", is_tv=True) == \
        pathlib.Path("/m/Shows/Show (2025)/Season 01/Show - S01E01.mkv")
    assert naming.library_path(pathlib.Path("/m/Movies/Heat (1995)"),
                               "/s/Heat (1995)/Heat (1995).mkv", is_tv=False) == \
        pathlib.Path("/m/Movies/Heat (1995)/Heat (1995).mkv")
    assert naming.library_dir_of("/m/Shows/Show/Season 01/x.mkv", is_tv=True) == \
        pathlib.Path("/m/Shows/Show")
    assert naming.library_dir_of("/m/Movies/Heat/Heat.mkv", is_tv=False) == \
        pathlib.Path("/m/Movies/Heat")


# ---------------------------------------------------------------------------
# DB states
# ---------------------------------------------------------------------------


def _job_with_dest(conn, tmp_path, *, status="queued", final=True) -> int:
    out = tmp_path / "staging" / "Heat" / "Heat.mkv"
    job_id = insert_job(conn, movie="Heat", status=status, output_path=str(out))
    if final:
        conn.execute("UPDATE jobs SET final_path = ? WHERE id = ?",
                     (str(tmp_path / "lib" / "Heat" / "Heat.mkv"), job_id))
        conn.commit()
    return job_id


def _move_row(conn, job_id):
    return conn.execute("SELECT status, move_state, move_error, final_path FROM jobs "
                        "WHERE id = ?", (job_id,)).fetchone()


def test_mark_done_hands_off_to_mover_only_with_destination(conn, tmp_path):
    with_dest = _job_with_dest(conn, tmp_path)
    without = _job_with_dest(conn, tmp_path, final=False)
    for job_id in (with_dest, without):
        db.mark_done(conn, job_id)
    assert _move_row(conn, with_dest)["move_state"] == "pending"
    assert _move_row(conn, without)["move_state"] is None


def test_claim_resets_move_state(conn, tmp_path):
    job_id = _job_with_dest(conn, tmp_path)
    conn.execute("UPDATE jobs SET move_state = 'failed', move_error = 'x' WHERE id = ?", (job_id,))
    conn.commit()
    job = db.claim_next_job(conn)
    assert job.id == job_id and (job.move_state, job.move_error) == (None, None)
    assert job.final_path.endswith("lib/Heat/Heat.mkv")


def test_reconcile_moves(conn, tmp_path):
    job_id = _job_with_dest(conn, tmp_path, status="done")
    conn.execute("UPDATE jobs SET move_state = 'moving'")
    assert db.reconcile_moves(conn) == 1
    assert _move_row(conn, job_id)["move_state"] == "pending"


@pytest.mark.parametrize("status,state,final,given,ok", [
    ("done", "failed", True, None, True),
    ("done", None, False, None, False),  # nowhere to go
    ("done", None, False, "/lib/x.mkv", True),
    ("done", "moving", True, None, False),  # already moving
    ("queued", None, True, None, False),  # not encoded yet
])
def test_request_move(conn, tmp_path, status, state, final, given, ok):
    job_id = _job_with_dest(conn, tmp_path, status=status, final=final)
    conn.execute("UPDATE jobs SET move_state = ?", (state,))
    assert db.request_move(conn, job_id, given) is ok
    if ok:
        row = _move_row(conn, job_id)
        assert row["move_state"] == "pending" and row["move_error"] is None
        if given:
            assert row["final_path"] == given


# ---------------------------------------------------------------------------
# Moving files
# ---------------------------------------------------------------------------


def test_move_file_same_filesystem(tmp_path):
    src = tmp_path / "a" / "x.mkv"
    src.parent.mkdir()
    src.write_text("video")
    dst = tmp_path / "lib" / "Show" / "Season 01" / "x.mkv"
    mover.move_file(src, dst)
    assert dst.read_text() == "video" and not src.exists()


def test_move_file_across_filesystems(tmp_path, monkeypatch):
    src = tmp_path / "x.mkv"
    src.write_text("video")
    dst = tmp_path / "lib" / "x.mkv"
    dst.parent.mkdir()
    dst.write_text("old encode")  # replaced, as a re-encode would
    real_replace = os.replace
    calls = []

    def replace(a, b):
        calls.append((pathlib.Path(a).name, pathlib.Path(b).name))
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(mover.os, "replace", replace)
    mover.move_file(src, dst)
    assert calls == [("x.mkv", "x.mkv"), ("x.mkv.partial", "x.mkv")]
    assert dst.read_text() == "video" and not src.exists()
    assert not (dst.parent / "x.mkv.partial").exists()


def test_move_file_failed_copy_leaves_no_partial(tmp_path, monkeypatch):
    src = tmp_path / "x.mkv"
    src.write_text("video")
    dst = tmp_path / "lib" / "x.mkv"
    monkeypatch.setattr(mover.os, "replace",
                        lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "xdev")))
    monkeypatch.setattr(mover.shutil, "copy2",
                        lambda a, b: (pathlib.Path(b).write_text("half"),
                                      (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space"))))
    with pytest.raises(OSError, match="No space"):
        mover.move_file(src, dst)
    assert src.exists() and not dst.exists()
    assert not (tmp_path / "lib" / "x.mkv.partial").exists()


def _ready(conn, tmp_path, *, src_exists=True, dst_exists=False) -> tuple[int, pathlib.Path, pathlib.Path]:
    job_id = _job_with_dest(conn, tmp_path, status="done")
    src = tmp_path / "staging" / "Heat" / "Heat.mkv"
    dst = tmp_path / "lib" / "Heat" / "Heat.mkv"
    if src_exists:
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("video")
    if dst_exists:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("video")
    conn.execute("UPDATE jobs SET move_state = 'pending'")
    conn.commit()
    return job_id, src, dst


def test_move_one(conn, tmp_path):
    job_id, src, dst = _ready(conn, tmp_path)
    assert mover.move_one(conn) is True
    assert _move_row(conn, job_id)["move_state"] == "moved"
    assert dst.exists() and not src.exists()
    assert mover.move_one(conn) is False  # nothing left


def test_move_one_already_moved_before_crash(conn, tmp_path):
    job_id, _, _ = _ready(conn, tmp_path, src_exists=False, dst_exists=True)
    mover.move_one(conn)
    assert _move_row(conn, job_id)["move_state"] == "moved"


def test_move_one_missing_file_fails(conn, tmp_path):
    job_id, src, _ = _ready(conn, tmp_path, src_exists=False)
    mover.move_one(conn)
    row = _move_row(conn, job_id)
    assert row["move_state"] == "failed" and f"encoded file not found: {src}" in row["move_error"]


def test_encode_then_move_end_to_end(tmp_path):
    """Worker path: encode (fake HandBrake) → done → mover → file in the library."""
    conn = db.connect(str(tmp_path / "jobs.db"))
    db.init_db(conn)
    source = tmp_path / "raw.mkv"
    source.touch()
    out = tmp_path / "staging" / "Show (2025)" / "Season 01" / "Show - S01E01.mkv"
    final = tmp_path / "lib" / "Show (2025)" / "Season 01" / "Show - S01E01.mkv"
    job_id = insert_job(conn, show="Show", season=1, episode=1, source_path=str(source),
                        output_path=str(out),
                        handbrake_args=HandbrakeArgs(audio_tracks=[AudioTrack(1)]).to_json())
    conn.execute("UPDATE jobs SET final_path = ?", (str(final),))
    conn.commit()
    job = db.claim_next_job(conn)
    with patch("rne.config.HANDBRAKE_PREFIX", [str(FIXTURES / "fake_encoder_ok.sh")]):
        run_job(job, conn)
    assert _move_row(conn, job_id)["move_state"] == "pending" and out.exists()
    mover.move_one(conn)
    assert _move_row(conn, job_id)["move_state"] == "moved"
    assert final.exists() and not out.exists()
    assert source.exists()  # raws are never touched


def test_job_from_row_without_move_columns():
    row = {"id": 1, "source_path": "s", "output_path": "o",
           "handbrake_args": HandbrakeArgs().to_json(), "status": "queued",
           "attempt_count": 0, "priority": 0, "show": None, "season": None,
           "episode": None, "movie": "M", "progress_pct": None, "progress_fps": None,
           "progress_eta": None, "created_at": "", "started_at": None, "finished_at": None,
           "exit_code": None, "error_message": None, "ingest_batch_id": None}

    class Row(dict):
        def keys(self):
            return list(super().keys())

    job = Job.from_row(Row(row))
    assert (job.final_path, job.move_state) == (None, None)


# ---------------------------------------------------------------------------
# Draft / Review
# ---------------------------------------------------------------------------


def _draft(tmp_path, **kw) -> Draft:
    src = tmp_path / "a.mkv"
    src.touch()
    d = Draft(mode="queue", naming=Naming(title="Show", season=1, first_episode=1, year=2025),
              staging_root=tmp_path / "staging", sources=[Source(src)], **kw)
    d.summaries = {src: SUMMARY}
    return d


def test_draft_moves_by_default_when_root_configured(tmp_path):
    d = _draft(tmp_path, library=LibraryConfig(tv_root=pathlib.Path("/m/Shows")))
    assert d.moving and d.library_dir == pathlib.Path("/m/Shows/Show (2025)")
    assert d.build_plan()[0]["final_path"] == "/m/Shows/Show (2025)/Season 01/Show - S01E01.mkv"
    d.move = False
    assert d.build_plan()[0]["final_path"] is None


def test_draft_without_root(tmp_path):
    d = _draft(tmp_path)
    assert not d.moving and d.build_plan()[0]["final_path"] is None
    d.move = True
    assert "library folder: none set ([library] has no root for this kind)" in d.blocking_errors()
    d.library_override = pathlib.Path("/elsewhere/Show")
    assert d.blocking_errors() == []
    assert d.build_plan()[0]["final_path"] == "/elsewhere/Show/Season 01/Show - S01E01.mkv"


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


def _to_review_app(conn, tmp_path, library):
    d = _draft(tmp_path, library=library)
    return PipelineApp(d, conn=conn, prober=lambda p: SUMMARY, chapter_prober=lambda p: [])


async def _go_to_review(pilot, app):
    await pilot.press("ctrl+n")  # naming → tracks
    await _settle(pilot)
    assert isinstance(app.screen, TracksScreen)
    await pilot.press("ctrl+n")
    await pilot.pause()
    assert isinstance(app.screen, ReviewScreen)


def test_review_moves_to_configured_library(conn, tmp_path):
    app = _to_review_app(conn, tmp_path, LibraryConfig(tv_root=tmp_path / "Shows"))

    async def steps(pilot, app):
        await _go_to_review(pilot, app)
        review = app.screen
        assert review.query_one("#move", Checkbox).value
        assert review.query_one("#library", Input).value == str(tmp_path / "Shows" / "Show (2025)")
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    assert lines[1] == f"Encodes will be moved to {tmp_path / 'Shows' / 'Show (2025)'}/ when done."
    final = conn.execute("SELECT final_path FROM jobs").fetchone()[0]
    assert final == str(tmp_path / "Shows" / "Show (2025)" / "Season 01" / "Show - S01E01.mkv")


def test_review_toggle_off_and_custom_folder(conn, tmp_path):
    app = _to_review_app(conn, tmp_path, LibraryConfig(tv_root=tmp_path / "Shows"))

    async def steps(pilot, app):
        await _go_to_review(pilot, app)
        await pilot.press("l")  # off
        await pilot.pause()
        assert not app.draft.moving and not app.screen.query_one("#library-row").display
        await pilot.press("l")  # back on
        await pilot.pause()
        app.screen.query_one("#library", Input).value = "relative/path"
        await pilot.pause()
        assert app.screen.query_one("#queue").disabled
        app.screen.query_one("#library", Input).value = str(tmp_path / "Anime" / "Show")
        await pilot.pause()
        await pilot.press("ctrl+s")

    _run(app, steps)
    final = conn.execute("SELECT final_path FROM jobs").fetchone()[0]
    assert final == str(tmp_path / "Anime" / "Show" / "Season 01" / "Show - S01E01.mkv")


def test_review_off_by_default_without_root(conn, tmp_path):
    app = _to_review_app(conn, tmp_path, LibraryConfig())

    async def steps(pilot, app):
        await _go_to_review(pilot, app)
        assert not app.screen.query_one("#move", Checkbox).value
        await pilot.press("ctrl+s")

    _run(app, steps)
    assert conn.execute("SELECT final_path FROM jobs").fetchone()[0] is None


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


def _tv_job_with_final(conn, tmp_path):
    out = tmp_path / "staging" / "Show" / "Season 01" / "Show - S01E01.mkv"
    final = tmp_path / "lib" / "Show" / "Season 01" / "Show - S01E01.mkv"
    job_id = insert_job(conn, show="Show", season=1, episode=1, output_path=str(out))
    conn.execute("UPDATE jobs SET final_path = ?, source_probe = ?",
                 (str(final), probe.snapshot_to_json(SUMMARY)))
    conn.commit()
    return job_id


def test_plan_edits_rename_repoints_library_folder(conn, tmp_path):
    job_id = _tv_job_with_final(conn, tmp_path)
    targets = jobs.load_edit_targets(conn, [job_id])
    (r,) = jobs.plan_edits(targets, template=None, title="Show", year=2025, first_episode=4)
    assert r.final_path == str(tmp_path / "lib" / "Show (2025)" / "Season 01" / "Show - S01E04.mkv")
    (r,) = jobs.plan_edits(targets, template=None, title="Show", library=None)
    assert r.final_path is None


def test_edit_screen_turn_move_off(conn, tmp_path):
    job_id = _tv_job_with_final(conn, tmp_path)

    async def steps(pilot, app):
        screen = app.screen
        assert screen.query_one("#move", Checkbox).value
        assert screen.query_one("#library", Input).value == str(tmp_path / "lib" / "Show")
        assert "⇒" in str(screen.query_one("#outputs").render())
        screen.query_one("#move", Checkbox).value = False
        await pilot.pause()
        assert "⇒" not in str(screen.query_one("#outputs").render())
        await pilot.press("ctrl+s")

    _run(EditApp(conn, [job_id]), steps)
    assert conn.execute("SELECT final_path FROM jobs").fetchone()[0] is None


def test_edit_screen_turn_move_on_from_config(conn, tmp_path):
    job_id = insert_job(conn, movie="Heat", output_path=str(tmp_path / "s" / "Heat" / "Heat.mkv"))
    conn.execute("UPDATE jobs SET source_probe = ?", (probe.snapshot_to_json(SUMMARY),))
    conn.commit()

    async def steps(pilot, app):
        screen = app.screen
        assert not screen.query_one("#move", Checkbox).value
        assert screen.query_one("#library", Input).value == str(tmp_path / "Movies" / "Heat")
        screen.query_one("#move", Checkbox).value = True
        await pilot.pause()
        await pilot.press("ctrl+s")

    _run(EditApp(conn, [job_id], library=LibraryConfig(movies_root=tmp_path / "Movies")), steps)
    assert conn.execute("SELECT final_path FROM jobs").fetchone()[0] == \
        str(tmp_path / "Movies" / "Heat" / "Heat.mkv")


# ---------------------------------------------------------------------------
# CLI and dashboard
# ---------------------------------------------------------------------------


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    conn = db.connect(path)
    db.init_db(conn)
    yield conn
    conn.close()


def test_cli_move_uses_config_root(file_db, tmp_path, monkeypatch, capsys):
    from rne.cli.manage import run_move

    job_id = insert_job(file_db, show="Show", season=1, episode=2, status="done",
                        output_path="/s/Show/Season 01/Show - S01E02.mkv")
    file_db.execute("UPDATE jobs SET year = 2025")
    file_db.commit()
    monkeypatch.setattr(config, "load_library_config",
                        lambda path=None: LibraryConfig(tv_root=pathlib.Path("/m/Shows")))
    run_move(types.SimpleNamespace(id=job_id, to=None))
    row = _move_row(file_db, job_id)
    assert row["move_state"] == "pending"
    assert row["final_path"] == "/m/Shows/Show (2025)/Season 01/Show - S01E02.mkv"
    assert "queued" in capsys.readouterr().out


def test_cli_move_to_and_refusals(file_db, monkeypatch):
    from rne.cli.manage import run_move

    done = insert_job(file_db, movie="Heat", status="done", output_path="/s/Heat/Heat.mkv")
    run_move(types.SimpleNamespace(id=done, to="/m/Films/Heat"))
    assert _move_row(file_db, done)["final_path"] == "/m/Films/Heat/Heat.mkv"

    queued = insert_job(file_db, movie="Heat", status="queued")
    with pytest.raises(SystemExit):
        run_move(types.SimpleNamespace(id=queued, to=None))
    monkeypatch.setattr(config, "load_library_config", lambda path=None: LibraryConfig())
    nowhere = insert_job(file_db, movie="X", status="done")
    with pytest.raises(SystemExit):
        run_move(types.SimpleNamespace(id=nowhere, to=None))


def test_cli_retry_refused_while_moving(file_db):
    from rne.cli.manage import run_retry

    job_id = insert_job(file_db, movie="Heat", status="done")
    file_db.execute("UPDATE jobs SET final_path = '/m/x.mkv', move_state = 'moving'")
    file_db.commit()
    with pytest.raises(SystemExit):
        run_retry(types.SimpleNamespace(id=job_id))
    assert _move_row(file_db, job_id)["status"] == "done"


def test_dashboard_retry_move(file_db):
    from rne.dashboard.app import create_app

    job_id = insert_job(file_db, movie="Heat", status="done")
    file_db.execute("UPDATE jobs SET final_path = '/m/Heat/Heat.mkv', move_state = 'failed', "
                    "move_error = 'No space left on device', finished_at = CURRENT_TIMESTAMP")
    file_db.commit()
    client = create_app().test_client()
    page = client.get("/").get_data(as_text=True)
    assert "move failed: No space left on device" in page and "Retry move" in page
    assert client.post(f"/jobs/{job_id}/move").status_code == 303
    assert _move_row(file_db, job_id)["move_state"] == "pending"
    assert "moving to library" in client.get("/").get_data(as_text=True)
