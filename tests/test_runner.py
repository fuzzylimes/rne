from __future__ import annotations

import pathlib
import sqlite3

import pytest

from rne import db
from rne.models import HandbrakeArgs, Job
from rne.worker.runner import parse_handbrake_progress, run_job

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# parse_handbrake_progress
# ---------------------------------------------------------------------------


def test_parse_typical_line():
    line = "Encoding: task 1 of 1, 12.34 % (45.6 fps, avg 50.0 fps, ETA 00h05m12s)"
    result = parse_handbrake_progress(line)
    assert result is not None
    assert result["pct"] == pytest.approx(12.34)
    assert result["fps"] == pytest.approx(45.6)
    assert result["eta"] == 5 * 60 + 12  # 312


def test_parse_zero_percent():
    line = "Encoding: task 1 of 1, 0.00 % (0.0 fps, avg 0.0 fps, ETA 00h10m00s)"
    result = parse_handbrake_progress(line)
    assert result is not None
    assert result["pct"] == pytest.approx(0.0)
    assert result["fps"] == pytest.approx(0.0)
    assert result["eta"] == 600


def test_parse_hundred_percent():
    line = "Encoding: task 1 of 1, 100.00 % (52.3 fps, avg 51.1 fps, ETA 00h00m00s)"
    result = parse_handbrake_progress(line)
    assert result is not None
    assert result["pct"] == pytest.approx(100.0)
    assert result["eta"] == 0


def test_parse_multi_task():
    line = "Encoding: task 2 of 3, 75.50 % (30.0 fps, avg 28.5 fps, ETA 01h02m03s)"
    result = parse_handbrake_progress(line)
    assert result is not None
    assert result["pct"] == pytest.approx(75.5)
    assert result["eta"] == 3600 + 2 * 60 + 3  # 3723


def test_parse_no_match_returns_none():
    assert parse_handbrake_progress("") is None
    assert parse_handbrake_progress("some random log line") is None
    assert parse_handbrake_progress("Muxing: this is not a progress line") is None


def test_parse_eta_only_hours():
    line = "Encoding: task 1 of 1, 1.00 % (5.0 fps, avg 5.0 fps, ETA 02h00m00s)"
    result = parse_handbrake_progress(line)
    assert result is not None
    assert result["eta"] == 7200


# ---------------------------------------------------------------------------
# run_job fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.init_db(c)
    yield c
    c.close()


def _insert_job(conn, source_path: str, output_path: str) -> Job:
    args = HandbrakeArgs()
    cur = conn.execute(
        """
        INSERT INTO jobs (movie, source_path, output_path, handbrake_args, status)
        VALUES (?, ?, ?, ?, 'running')
        """,
        ("Test Movie", source_path, output_path, args.to_json()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return Job.from_row(row)


# ---------------------------------------------------------------------------
# run_job — success path
# ---------------------------------------------------------------------------


def test_run_job_success(mem_conn, tmp_path):
    encoder = str(FIXTURES / "fake_encoder_ok.sh")
    source = tmp_path / "source.mkv"
    source.touch()
    output = tmp_path / "out" / "output.mkv"

    job = _insert_job(mem_conn, str(source), str(output))

    # Override handbrake_args so build_command uses our fake script.
    from rne.models import HandbrakeArgs, AudioTrack
    from unittest.mock import patch

    fake_args = HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")])
    job.handbrake_args = fake_args

    with patch("rne.config.HANDBRAKE_PREFIX", [encoder]):
        run_job(job, mem_conn)

    row = mem_conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row["status"] == "done"
    assert row["exit_code"] is None
    assert output.exists()


def test_run_job_success_records_progress(mem_conn, tmp_path):
    """Progress rows are written during encode (throttle may suppress intermediate ones)."""
    encoder = str(FIXTURES / "fake_encoder_ok.sh")
    source = tmp_path / "source.mkv"
    source.touch()
    output = tmp_path / "output.mkv"

    job = _insert_job(mem_conn, str(source), str(output))
    from rne.models import AudioTrack, HandbrakeArgs
    from unittest.mock import patch

    job.handbrake_args = HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")])

    # Reduce the throttle so all three lines in the fake script get written.
    with (
        patch("rne.config.HANDBRAKE_PREFIX", [encoder]),
        patch("rne.worker.runner.time") as mock_time,
    ):
        # monotonic always returns a value far enough apart to bypass throttle.
        mock_time.monotonic.side_effect = [0.0, 999.0, 1999.0, 2999.0, 3999.0]
        run_job(job, mem_conn)

    row = mem_conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row["status"] == "done"
    # mark_done sets progress_pct to 100.0
    assert row["progress_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# run_job — failure path
# ---------------------------------------------------------------------------


def test_run_job_failure(mem_conn, tmp_path):
    encoder = str(FIXTURES / "fake_encoder_fail.sh")
    source = tmp_path / "source.mkv"
    source.touch()
    output = tmp_path / "output.mkv"

    job = _insert_job(mem_conn, str(source), str(output))
    from rne.models import AudioTrack, HandbrakeArgs
    from unittest.mock import patch

    job.handbrake_args = HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")])

    with patch("rne.config.HANDBRAKE_PREFIX", [encoder]):
        run_job(job, mem_conn)

    row = mem_conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row["status"] == "failed"
    assert row["exit_code"] == 1
    assert row["error_message"] is not None
    assert "ERROR" in row["error_message"] or "libav" in row["error_message"]
    # .partial must not be left behind (it's not created by the fail script)
    assert not (tmp_path / "output.mkv.partial").exists()


def test_run_job_failure_no_output_file(mem_conn, tmp_path):
    """On failure the final output file must not exist."""
    encoder = str(FIXTURES / "fake_encoder_fail.sh")
    source = tmp_path / "source.mkv"
    source.touch()
    output = tmp_path / "output.mkv"

    job = _insert_job(mem_conn, str(source), str(output))
    from rne.models import AudioTrack, HandbrakeArgs
    from unittest.mock import patch

    job.handbrake_args = HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")])

    with patch("rne.config.HANDBRAKE_PREFIX", [encoder]):
        run_job(job, mem_conn)

    assert not output.exists()
