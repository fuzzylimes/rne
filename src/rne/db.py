from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from rne import config

if TYPE_CHECKING:
    from rne.models import Job

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingest_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    show        TEXT,
    movie       TEXT,
    notes       TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    show              TEXT,
    season            INTEGER,
    episode           INTEGER,
    movie             TEXT,

    source_path       TEXT NOT NULL,
    output_path       TEXT NOT NULL,

    handbrake_args    TEXT NOT NULL,

    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','paused','running',
                                        'done','failed','cancelled','interrupted')),
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    priority          INTEGER NOT NULL DEFAULT 0,

    progress_pct      REAL,
    progress_fps      REAL,
    progress_eta      INTEGER,

    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,

    exit_code         INTEGER,
    error_message     TEXT,

    ingest_batch_id   INTEGER REFERENCES ingest_batches(id),

    CHECK ((show IS NOT NULL AND movie IS NULL) OR (show IS NULL AND movie IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_claim  ON jobs(priority, id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_jobs_batch  ON jobs(ingest_batch_id);

CREATE TABLE IF NOT EXISTS worker_status (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    pid             INTEGER,
    state           TEXT NOT NULL DEFAULT 'starting'
                    CHECK (state IN ('starting','idle','encoding','stopping')),
    current_job_id  INTEGER REFERENCES jobs(id),
    last_seen       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO worker_status (id) VALUES (1);

CREATE TABLE IF NOT EXISTS queue_settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    paused  INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1))
);
INSERT OR IGNORE INTO queue_settings (id) VALUES (1);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Worker queries
# ---------------------------------------------------------------------------

def claim_next_job(conn: sqlite3.Connection) -> "Job | None":
    from rne.models import Job

    row = conn.execute("""
        UPDATE jobs
        SET    status        = 'running',
               started_at    = CURRENT_TIMESTAMP,
               attempt_count = attempt_count + 1,
               progress_pct  = NULL,
               progress_fps  = NULL,
               progress_eta  = NULL,
               exit_code     = NULL,
               error_message = NULL
        WHERE  id = (
            SELECT id FROM jobs
            WHERE  status = 'queued'
            ORDER BY priority ASC, id ASC
            LIMIT  1
        )
        RETURNING *
    """).fetchone()
    if row is None:
        return None
    conn.commit()
    return Job.from_row(row)


def reconcile_orphans(conn: sqlite3.Connection) -> int:
    cur = conn.execute("""
        UPDATE jobs
        SET    status        = 'interrupted',
               finished_at   = CURRENT_TIMESTAMP,
               error_message = 'worker did not finish; reconciled on startup'
        WHERE  status = 'running'
    """)
    conn.commit()
    return cur.rowcount


def update_progress(
    conn: sqlite3.Connection,
    job_id: int,
    pct: float,
    fps: float,
    eta: int,
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET progress_pct = ?, progress_fps = ?, progress_eta = ?
        WHERE id = ?
        """,
        (pct, fps, eta, job_id),
    )
    conn.commit()


def mark_done(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = 'done', finished_at = CURRENT_TIMESTAMP,
            progress_pct = 100.0, progress_fps = NULL, progress_eta = NULL
        WHERE id = ?
        """,
        (job_id,),
    )
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection,
    job_id: int,
    exit_code: int,
    error_message: str = "",
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = 'failed', finished_at = CURRENT_TIMESTAMP,
            exit_code = ?, error_message = ?
        WHERE id = ?
        """,
        (exit_code, error_message, job_id),
    )
    conn.commit()
