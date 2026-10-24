"""Tests for schema migrations, probe snapshots and rne.jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
from unittest.mock import patch

import pytest

from rne import db, jobs, plan, probe
from rne.cli import _pipeline
from rne.models import AudioTrack, HandbrakeArgs
from rne.probe import AudioStream, Chapter, StreamSummary, SubtitleStream, VideoStream
from tests.conftest import insert_job

SUMMARY = StreamSummary(
    video=[VideoStream("h264", "1920x1080", "23.976", "progressive", "eng", True, False)],
    audio=[
        AudioStream("truehd", 6, "eng", "Main", True, False, None),
        AudioStream("ac3", 2, "eng", "Commentary", False, False, 192000),
    ],
    subtitle=[SubtitleStream("hdmv_pgs_subtitle", "eng", "", False, False, 812)],
)
CHAPTERS = [Chapter(1, 0.0, 1400.5, "Chapter 01"), Chapter(2, 1400.5, 2800.0, "Chapter 02")]


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_fresh_db_is_at_latest_version(conn):
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert "source_probe" in _columns(conn, "jobs")


def test_pre_v06_db_is_upgraded_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA_SQL)  # v0.5 schema, user_version 0
    old.execute(
        "INSERT INTO jobs (movie, source_path, output_path, handbrake_args) VALUES (?,?,?,?)",
        ("Heat", "/r/a.mkv", "/s/Heat.mkv", HandbrakeArgs().to_json()),
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    db.init_db(conn)
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    row = conn.execute("SELECT movie, source_probe FROM jobs").fetchone()
    assert (row["movie"], row["source_probe"]) == ("Heat", None)


def test_migrate_is_idempotent(conn):
    db.init_db(conn)
    db.migrate(conn)
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_concurrent_init_db_does_not_fail(tmp_path):
    path = str(tmp_path / "race.db")
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def worker():
        try:
            c = db.connect(path)
            start.wait(timeout=10)
            db.init_db(c)
            c.close()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert
            errors.append(exc)
            start.abort()  # never leave the other threads waiting

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS)


def test_failed_migration_rolls_back(conn, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [("ALTER TABLE nope ADD x",)])
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    assert db.schema_version(conn) == len(db.MIGRATIONS) - 1


# ---------------------------------------------------------------------------
# Probe snapshot
# ---------------------------------------------------------------------------


def test_snapshot_roundtrip_without_chapters():
    assert probe.snapshot_from_json(probe.snapshot_to_json(SUMMARY)) == (SUMMARY, None)


def test_snapshot_roundtrip_with_chapters():
    assert probe.snapshot_from_json(probe.snapshot_to_json(SUMMARY, CHAPTERS)) == (
        SUMMARY,
        CHAPTERS,
    )


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"version": 99, "video": [], "audio": [], "subtitle": []}),
        json.dumps({"version": 1, "video": [], "audio": [{"codec": "ac3"}], "subtitle": []}),
        json.dumps({"version": 1, "video": []}),
    ],
)
def test_snapshot_rejects_bad_input(text):
    with pytest.raises(ValueError):
        probe.snapshot_from_json(text)


# ---------------------------------------------------------------------------
# insert_jobs
# ---------------------------------------------------------------------------


def _plan_entry(tmp_path, **extra) -> dict:
    job = plan.movie_job(movie="Heat", movie_dir=tmp_path / "Heat",
                         source_path="/r/a.mkv", args=HandbrakeArgs())
    job.update(extra)
    return job


def test_insert_jobs_stores_snapshot(conn, tmp_path):
    batch = jobs.create_batch_row(conn, is_tv=False, show=None, movie="Heat", season=None, notes="")
    jobs.insert_jobs(conn, batch, [_plan_entry(tmp_path, source_summary=SUMMARY, source_chapters=CHAPTERS)])
    row = conn.execute("SELECT source_probe, ingest_batch_id FROM jobs").fetchone()
    assert row["ingest_batch_id"] == batch
    assert probe.snapshot_from_json(row["source_probe"]) == (SUMMARY, CHAPTERS)
    assert (tmp_path / "Heat").is_dir()


def test_insert_jobs_without_summary_stores_null(conn, tmp_path):
    batch = jobs.create_batch_row(conn, is_tv=False, show=None, movie="Heat", season=None, notes="")
    jobs.insert_jobs(conn, batch, [_plan_entry(tmp_path)])
    assert conn.execute("SELECT source_probe FROM jobs").fetchone()[0] is None


# ---------------------------------------------------------------------------
# update_job_args
# ---------------------------------------------------------------------------


NEW_ARGS = HandbrakeArgs(quality=17, audio_tracks=[AudioTrack(2)])


@pytest.mark.parametrize("status", ["queued", "paused", "failed", "done", "interrupted", "cancelled"])
def test_update_job_args_allowed_states(conn, status):
    job_id = insert_job(conn, status=status)
    jobs.update_job_args(conn, job_id, NEW_ARGS)
    stored = conn.execute("SELECT handbrake_args FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    assert HandbrakeArgs.from_json(stored) == NEW_ARGS


def test_update_job_args_refuses_running(conn):
    job_id = insert_job(conn, status="running")
    with pytest.raises(jobs.JobRunning):
        jobs.update_job_args(conn, job_id, NEW_ARGS)
    stored = conn.execute("SELECT handbrake_args FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    assert HandbrakeArgs.from_json(stored) == HandbrakeArgs()


def test_update_job_args_missing_job(conn):
    with pytest.raises(jobs.JobNotFound):
        jobs.update_job_args(conn, 999, NEW_ARGS)


# ---------------------------------------------------------------------------
# load_source_summary
# ---------------------------------------------------------------------------


def _fake_probe_data() -> dict:
    return {"streams": [{"codec_type": "audio", "codec_name": "dts", "channels": 6}]}


def test_load_source_summary_uses_snapshot(conn):
    job_id = insert_job(conn)
    conn.execute("UPDATE jobs SET source_probe = ?", (probe.snapshot_to_json(SUMMARY, CHAPTERS),))

    def no_probe(path):
        raise AssertionError("should not probe")

    assert jobs.load_source_summary(conn, job_id, probe_file=no_probe) == (SUMMARY, CHAPTERS)


@pytest.mark.parametrize("stored", [None, "garbage"])
def test_load_source_summary_reprobes_and_saves(conn, stored):
    job_id = insert_job(conn, source_path="/r/legacy.mkv")
    conn.execute("UPDATE jobs SET source_probe = ?", (stored,))
    calls: list[str] = []

    def fake_probe(path):
        calls.append(path)
        return _fake_probe_data()

    summary, chapters = jobs.load_source_summary(conn, job_id, probe_file=fake_probe)
    assert calls == ["/r/legacy.mkv"]
    assert [a.codec for a in summary.audio] == ["dts"] and chapters is None
    # Saved, so the second call does not probe again.
    assert jobs.load_source_summary(conn, job_id, probe_file=fake_probe)[0] == summary
    assert len(calls) == 1


def test_load_source_summary_missing_job(conn):
    with pytest.raises(jobs.JobNotFound):
        jobs.load_source_summary(conn, 42)


# ---------------------------------------------------------------------------
# CLI pipeline carries the summary through preview and $EDITOR
# ---------------------------------------------------------------------------


def test_preview_and_confirm_attaches_summaries(tmp_path):
    other = StreamSummary(video=[], audio=SUMMARY.audio, subtitle=[])
    plan_ = [
        _plan_entry(tmp_path, source_path="/r/a.mkv"),
        _plan_entry(tmp_path, source_path="/r/b.mkv"),
    ]
    with (
        patch.object(_pipeline.probe, "probe", return_value={}),
        patch.object(_pipeline.probe, "summarize", return_value=other),
        patch("builtins.input", return_value="y"),
    ):
        result = _pipeline.preview_and_confirm(plan_, SUMMARY, [tmp_path / "b.mkv"])
    assert result[0]["source_summary"] is SUMMARY
    assert result[1]["source_summary"] is other


def test_disc_split_jobs_share_reference_summary(tmp_path):
    plan_ = [
        _plan_entry(tmp_path, source_path="/r/disc.mkv"),
        _plan_entry(tmp_path, source_path="/r/disc.mkv"),
    ]
    with patch("builtins.input", return_value="y"):
        result = _pipeline.preview_and_confirm(plan_, SUMMARY, [])
    assert all(j["source_summary"] is SUMMARY for j in result)


def test_edit_plan_round_trip_keeps_summaries(tmp_path):
    original = [_plan_entry(tmp_path, source_summary=SUMMARY, source_chapters=CHAPTERS)]

    def fake_editor(cmd, **kwargs):
        with open(cmd[1]) as f:
            data = json.load(f)
        assert "source_summary" not in data[0]  # probe objects stay out of the editor
        data[0]["handbrake_args"]["quality"] = 15
        with open(cmd[1], "w") as f:
            json.dump(data, f)

    with patch.object(_pipeline.subprocess, "run", side_effect=fake_editor):
        edited = _pipeline.edit_plan(original)
    assert edited[0]["handbrake_args"].quality == 15
    assert edited[0]["source_summary"] is SUMMARY
    assert edited[0]["source_chapters"] is CHAPTERS
