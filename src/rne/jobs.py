"""Job-level operations shared by the CLI, TUI, dashboard and API.

db.py owns the schema and the worker's queries; this module owns the
operations front ends perform on jobs: creating batches, inserting a job plan,
editing settings safely, and loading a job's source track layout.
"""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Callable

from rne import probe
from rne.models import HandbrakeArgs, JobStatus


class JobNotFound(LookupError):
    pass


class JobRunning(RuntimeError):
    """The worker owns this job; its settings cannot change until it finishes."""


def create_batch_row(
    conn: sqlite3.Connection,
    *,
    is_tv: bool,
    show: str | None,
    movie: str | None,
    season: int | None,
    notes: str,
    label_suffix: str = "",
) -> int:
    """Insert an ingest_batches row and return its id."""
    base_label = f"{show} S{season:02d}" if is_tv else str(movie)
    label = base_label + label_suffix
    cur = conn.execute(
        "INSERT INTO ingest_batches (label, show, movie, notes) VALUES (?, ?, ?, ?)",
        (label, show, movie, notes),
    )
    conn.commit()
    return cur.lastrowid


def insert_jobs(conn: sqlite3.Connection, batch_id: int, jobs_plan: list[dict]) -> None:
    """Insert job rows for an already-created ingest batch.

    Creates each output directory. Stores the source probe snapshot when the
    plan entry carries one (source_summary / source_chapters).
    """
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
        pathlib.Path(job["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        summary = job.get("source_summary")
        snapshot = (
            probe.snapshot_to_json(summary, job.get("source_chapters"))
            if summary is not None
            else None
        )
        conn.execute(
            """
            INSERT INTO jobs
              (show, season, episode, movie, source_path, output_path,
               handbrake_args, ingest_batch_id, source_probe)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["show"],
                job["season"],
                job["episode"],
                job["movie"],
                job["source_path"],
                job["output_path"],
                hb.to_json(),
                batch_id,
                snapshot,
            ),
        )
    conn.commit()


def update_job_args(conn: sqlite3.Connection, job_id: int, args: HandbrakeArgs) -> None:
    """Replace a job's encode settings unless the worker is running it.

    The status check and the write are one statement, so the worker cannot
    claim the job between them. Raises JobNotFound or JobRunning.
    """
    cur = conn.execute(
        "UPDATE jobs SET handbrake_args = ? WHERE id = ? AND status != ?",
        (args.to_json(), job_id, JobStatus.RUNNING.value),
    )
    conn.commit()
    if cur.rowcount == 1:
        return
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise JobNotFound(job_id)
    raise JobRunning(job_id)


def load_source_summary(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    probe_file: Callable[[str], dict] = probe.probe,
) -> tuple[probe.StreamSummary, list[probe.Chapter] | None]:
    """The job's source track layout, from its snapshot or a fresh probe.

    Jobs queued before v0.6 have no snapshot: the source is probed once and
    the result saved. Raises JobNotFound, or whatever probe_file raises when
    the source is gone or unreadable.
    """
    row = conn.execute(
        "SELECT source_path, source_probe FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise JobNotFound(job_id)
    if row["source_probe"]:
        try:
            return probe.snapshot_from_json(row["source_probe"])
        except ValueError:
            pass  # unreadable snapshot: fall through and re-probe
    summary = probe.summarize(probe_file(row["source_path"]))
    conn.execute(
        "UPDATE jobs SET source_probe = ? WHERE id = ?",
        (probe.snapshot_to_json(summary), job_id),
    )
    conn.commit()
    return summary, None
