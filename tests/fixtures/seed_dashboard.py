"""
Seed the database with jobs in various states for dashboard smoke-testing.

Usage:
    python tests/fixtures/seed_dashboard.py
    # or with a custom DB path:
    RNE_DB=/tmp/rne_test.db python tests/fixtures/seed_dashboard.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rne import config, db

ARGS = json.dumps(
    {
        "encoder": "x265",
        "quality": 20,
        "preset": "slow",
        "audio_tracks": [{"track": 1, "codec": "copy"}],
        "subtitle_tracks": [],
        "decomb": False,
        "extra_args": [],
    }
)

JOBS = [
    # (show, season, episode, movie, status, progress_pct, progress_fps, progress_eta, error_message, finished_at)
    ("Initial D", 1, 5, None, "running", 67.3, 42.1, 494, None, None),
    ("Initial D", 1, 6, None, "queued", None, None, None, None, None),
    ("Initial D", 1, 7, None, "queued", None, None, None, None, None),
    ("Initial D", 1, 8, None, "paused", None, None, None, None, None),
    ("Initial D", 1, 4, None, "done", 100, None, None, None, "2026-05-04 22:14:00"),
    (
        "Initial D",
        1,
        3,
        None,
        "failed",
        None,
        None,
        None,
        "HandBrake exit 1: invalid source",
        "2026-05-04 20:00:00",
    ),
    ("Initial D", 1, 2, None, "done", 100, None, None, None, "2026-05-04 18:02:00"),
    (
        None,
        None,
        None,
        "The Silence of the Lambs",
        "interrupted",
        None,
        None,
        None,
        "worker did not finish; reconciled on startup",
        "2026-05-04 10:00:00",
    ),
]


def seed() -> None:
    conn = db.connect()
    db.init_db(conn)

    conn.execute("DELETE FROM jobs")
    conn.execute("DELETE FROM ingest_batches")
    conn.execute(
        "UPDATE worker_status SET state='encoding', last_seen=CURRENT_TIMESTAMP WHERE id=1"
    )
    conn.execute("UPDATE queue_settings SET paused=0 WHERE id=1")

    batch_id = conn.execute(
        "INSERT INTO ingest_batches (label, show) VALUES (?, ?)",
        ("Initial D S01 Disc 1", "Initial D"),
    ).lastrowid
    conn.commit()

    for show, season, episode, movie, status, pct, fps, eta, err, finished in JOBS:
        conn.execute(
            """
            INSERT INTO jobs
                (show, season, episode, movie, source_path, output_path,
                 handbrake_args, status, progress_pct, progress_fps, progress_eta,
                 error_message, finished_at, ingest_batch_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                show,
                season,
                episode,
                movie,
                f"/mnt/media/staging/raw/title_t0{episode or 0}.mkv",
                f"/mnt/media/staging/output/ep{episode or 0}.mkv",
                ARGS,
                status,
                pct,
                fps,
                eta,
                err,
                finished,
                batch_id if show else None,
            ),
        )
    conn.commit()
    print(f"Seeded {len(JOBS)} jobs into {config.DB_PATH}")


if __name__ == "__main__":
    seed()
