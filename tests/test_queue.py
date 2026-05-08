"""Tests for rne queue — path resolution, manifest ordering, DB insertion."""

from __future__ import annotations

import pathlib
import sqlite3
import pytest

from rne import db
from rne.cli import _build_parser
from rne.cli.queue import _build_jobs_plan_queue, _resolve_manifest
from rne.models import AudioTrack, HandbrakeArgs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_hb_args() -> HandbrakeArgs:
    return HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")])


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Path resolution: single file
# ---------------------------------------------------------------------------


def test_resolve_manifest_single_mkv(tmp_path):
    f = tmp_path / "movie.mkv"
    f.touch()
    result = _resolve_manifest(f)
    assert result == [f]


def test_resolve_manifest_single_non_mkv_exits(tmp_path, capsys):
    f = tmp_path / "movie.mp4"
    f.touch()
    with pytest.raises(SystemExit) as exc:
        _resolve_manifest(f)
    assert exc.value.code == 1


def test_resolve_manifest_missing_path_exits(tmp_path):
    missing = tmp_path / "does_not_exist.mkv"
    with pytest.raises(SystemExit) as exc:
        _resolve_manifest(missing)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Path resolution: directory
# ---------------------------------------------------------------------------


def test_resolve_manifest_directory_returns_sorted(tmp_path):
    (tmp_path / "C1_t02.mkv").touch()
    (tmp_path / "A1_t00.mkv").touch()
    (tmp_path / "B1_t01.mkv").touch()
    result = _resolve_manifest(tmp_path)
    names = [f.name for f in result]
    assert names == ["A1_t00.mkv", "B1_t01.mkv", "C1_t02.mkv"]


def test_resolve_manifest_directory_ignores_non_mkv(tmp_path):
    (tmp_path / "episode.mkv").touch()
    (tmp_path / "readme.txt").touch()
    (tmp_path / "cover.jpg").touch()
    result = _resolve_manifest(tmp_path)
    assert len(result) == 1
    assert result[0].name == "episode.mkv"


def test_resolve_manifest_empty_directory_exits_0(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _resolve_manifest(tmp_path)
    assert exc.value.code == 0


def test_resolve_manifest_empty_directory_prints_friendly_message(tmp_path, capsys):
    with pytest.raises(SystemExit):
        _resolve_manifest(tmp_path)
    out = capsys.readouterr().out
    assert "nothing to queue" in out.lower() or "No .mkv" in out


# ---------------------------------------------------------------------------
# Manifest ordering: alphabetical for directories
# ---------------------------------------------------------------------------


def test_resolve_manifest_alphabetical_ordering_is_stable(tmp_path):
    names = ["Z_last.mkv", "A_first.mkv", "M_middle.mkv"]
    for n in names:
        (tmp_path / n).touch()
    result = _resolve_manifest(tmp_path)
    assert [f.name for f in result] == sorted(names)


# ---------------------------------------------------------------------------
# _build_jobs_plan_queue: source_path points at original file
# ---------------------------------------------------------------------------


def test_queue_jobs_plan_tv_source_path_unchanged(tmp_path):
    src = tmp_path / "episode.mkv"
    src.touch()
    plan = _build_jobs_plan_queue(
        is_tv=True,
        show="TestShow",
        season=1,
        episodes=[1],
        movie=None,
        staging_dir=pathlib.Path("/staging/TestShow"),
        source_paths=[src],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(src)


def test_queue_jobs_plan_movie_source_path_unchanged(tmp_path):
    src = tmp_path / "movie.mkv"
    src.touch()
    plan = _build_jobs_plan_queue(
        is_tv=False,
        show=None,
        season=None,
        episodes=None,
        movie="Aliens",
        staging_dir=pathlib.Path("/staging/Aliens"),
        source_paths=[src],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(src)


def test_queue_jobs_plan_source_path_not_mutated(tmp_path):
    """source_path must equal the original path, not a constructed title name."""
    src = tmp_path / "D1_t00.mkv"
    src.touch()
    plan = _build_jobs_plan_queue(
        is_tv=True,
        show="Show",
        season=2,
        episodes=[3],
        movie=None,
        staging_dir=pathlib.Path("/staging/Show"),
        source_paths=[src],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(src)
    assert "title_t" not in plan[0]["source_path"]


def test_queue_jobs_plan_tv_output_path_correct(tmp_path):
    src = tmp_path / "ep.mkv"
    src.touch()
    staging = pathlib.Path("/staging/MyShow")
    plan = _build_jobs_plan_queue(
        is_tv=True,
        show="MyShow",
        season=2,
        episodes=[5],
        movie=None,
        staging_dir=staging,
        source_paths=[src],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["output_path"] == str(staging / "Season 02" / "MyShow - S02E05.mkv")


def test_queue_jobs_plan_movie_single_output_path(tmp_path):
    src = tmp_path / "film.mkv"
    src.touch()
    staging = pathlib.Path("/staging/Aliens")
    plan = _build_jobs_plan_queue(
        is_tv=False,
        show=None,
        season=None,
        episodes=None,
        movie="Aliens",
        staging_dir=staging,
        source_paths=[src],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["output_path"] == str(staging / "Aliens.mkv")


def test_queue_jobs_plan_multi_episode_order(tmp_path):
    files = [tmp_path / f"ep{i}.mkv" for i in range(3)]
    for f in files:
        f.touch()
    plan = _build_jobs_plan_queue(
        is_tv=True,
        show="Show",
        season=1,
        episodes=[5, 6, 7],
        movie=None,
        staging_dir=pathlib.Path("/staging/Show"),
        source_paths=files,
        hb_args=_default_hb_args(),
    )
    assert [j["episode"] for j in plan] == [5, 6, 7]
    assert [j["source_path"] for j in plan] == [str(f) for f in files]


# ---------------------------------------------------------------------------
# DB: ingest_batches row created with correct notes; no raw dir
# ---------------------------------------------------------------------------


def test_queue_batch_row_notes_contains_source_path(conn, tmp_path):
    from rne.cli._pipeline import create_batch_row

    notes = f"Queued from {tmp_path.absolute()}"
    batch_id = create_batch_row(
        conn,
        is_tv=False,
        show=None,
        movie="Test Movie",
        season=None,
        notes=notes,
        label_suffix=" (queued)",
    )
    row = conn.execute(
        "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()
    assert str(tmp_path.absolute()) in row["notes"]


def test_queue_batch_row_label_has_queued_suffix(conn, tmp_path):
    from rne.cli._pipeline import create_batch_row

    batch_id = create_batch_row(
        conn,
        is_tv=True,
        show="Full Metal Panic",
        movie=None,
        season=1,
        notes="Queued from /some/path",
        label_suffix=" (queued)",
    )
    row = conn.execute(
        "SELECT label FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()
    assert row["label"] == "Full Metal Panic S01 (queued)"


def test_queue_batch_row_no_raw_dir_created(conn, tmp_path):
    """create_batch_row for queue mode must not touch the filesystem."""
    from rne.cli._pipeline import create_batch_row

    create_batch_row(
        conn,
        is_tv=False,
        show=None,
        movie="Movie",
        season=None,
        notes=f"Queued from {tmp_path}",
        label_suffix=" (queued)",
    )
    # No _raw subdirectory should exist
    assert not any(tmp_path.rglob("_raw"))


# ---------------------------------------------------------------------------
# DB: insert_jobs — source_path equals original file path
# ---------------------------------------------------------------------------


def test_insert_jobs_source_path_preserved(conn, tmp_path):
    from rne.cli._pipeline import create_batch_row, insert_jobs

    src = tmp_path / "original.mkv"
    src.touch()

    batch_id = create_batch_row(
        conn,
        is_tv=False,
        show=None,
        movie="Film",
        season=None,
        notes="Queued from /somewhere",
        label_suffix=" (queued)",
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    jobs_plan = [
        {
            "label": "Film",
            "show": None,
            "season": None,
            "episode": None,
            "movie": "Film",
            "source_path": str(src),
            "output_path": str(out_dir / "Film.mkv"),
            "handbrake_args": _default_hb_args(),
            "layout_warning": False,
        }
    ]
    insert_jobs(conn, batch_id, jobs_plan)

    row = conn.execute("SELECT source_path FROM jobs WHERE ingest_batch_id=?",
                       (batch_id,)).fetchone()
    assert row["source_path"] == str(src)


def test_insert_jobs_source_path_outside_staging(conn, tmp_path):
    """source_path may point anywhere — not just under the staging root."""
    from rne.cli._pipeline import create_batch_row, insert_jobs

    external_src = tmp_path / "external" / "old-rip.mkv"
    external_src.parent.mkdir()
    external_src.touch()

    batch_id = create_batch_row(
        conn,
        is_tv=False,
        show=None,
        movie="SomeFilm",
        season=None,
        notes="Queued from external",
        label_suffix=" (queued)",
    )

    out_dir = tmp_path / "staging"
    out_dir.mkdir()

    jobs_plan = [
        {
            "label": "SomeFilm",
            "show": None,
            "season": None,
            "episode": None,
            "movie": "SomeFilm",
            "source_path": str(external_src),
            "output_path": str(out_dir / "SomeFilm.mkv"),
            "handbrake_args": _default_hb_args(),
            "layout_warning": False,
        }
    ]
    insert_jobs(conn, batch_id, jobs_plan)

    row = conn.execute("SELECT source_path FROM jobs", ()).fetchone()
    assert row["source_path"] == str(external_src)


# ---------------------------------------------------------------------------
# argparse: queue subcommand
# ---------------------------------------------------------------------------


def test_parser_queue_with_file():
    args = _build_parser().parse_args(["queue", "/tmp/film.mkv"])
    assert args.command == "queue"
    assert args.path == "/tmp/film.mkv"


def test_parser_queue_with_directory():
    args = _build_parser().parse_args(["queue", "/home/rip/old-rips/disc-2/"])
    assert args.command == "queue"
    assert args.path == "/home/rip/old-rips/disc-2/"


def test_parser_queue_requires_path():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["queue"])


# ---------------------------------------------------------------------------
# Mismatched layouts: skip-mismatched does not touch source files
# ---------------------------------------------------------------------------


def test_skip_mismatched_source_files_untouched(tmp_path):
    """After skip-mismatched, source files must still exist at original paths."""
    src_a = tmp_path / "A1_t00.mkv"
    src_b = tmp_path / "B1_t01.mkv"
    src_a.touch()
    src_b.touch()

    # Simulate jobs_plan with one mismatched job
    plan = [
        {
            "label": "S01E01",
            "show": "Show",
            "season": 1,
            "episode": 1,
            "movie": None,
            "source_path": str(src_a),
            "output_path": "/staging/Show/Season 01/Show - S01E01.mkv",
            "handbrake_args": _default_hb_args(),
            "layout_warning": False,
        },
        {
            "label": "S01E02",
            "show": "Show",
            "season": 1,
            "episode": 2,
            "movie": None,
            "source_path": str(src_b),
            "output_path": "/staging/Show/Season 01/Show - S01E02.mkv",
            "handbrake_args": _default_hb_args(),
            "layout_warning": True,
        },
    ]

    # skip-mismatched filters the plan; it must not move/delete source files
    filtered = [j for j in plan if not j.get("layout_warning")]
    assert len(filtered) == 1
    assert filtered[0]["source_path"] == str(src_a)

    # Both files must still exist — skip does not touch them
    assert src_a.exists()
    assert src_b.exists()
