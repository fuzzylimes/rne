from __future__ import annotations

import datetime

from flask import Flask, g, redirect, render_template

TERMINAL_STATUSES = ("done", "failed", "cancelled", "interrupted")


def _worker_status() -> dict:
    row = g.conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()
    if row is None:
        return {
            "state": "unknown",
            "last_seen": None,
            "indicator": "offline",
            "ago": None,
        }

    last_seen_str = row["last_seen"]
    try:
        last_seen = datetime.datetime.fromisoformat(last_seen_str)
        # SQLite stores in local time without tz; treat as UTC for age calc
        age = (datetime.datetime.utcnow() - last_seen).total_seconds()
    except (TypeError, ValueError):
        age = None

    if age is None or age > 120:
        indicator = "offline"
    elif age > 90:
        indicator = "red"
    elif age > 30:
        indicator = "amber"
    else:
        indicator = "green"

    return {
        "state": row["state"],
        "last_seen": last_seen_str,
        "indicator": indicator,
        "ago": int(age) if age is not None else None,
        "current_job_id": row["current_job_id"],
    }


def _queue_paused() -> bool:
    row = g.conn.execute("SELECT paused FROM queue_settings WHERE id = 1").fetchone()
    return bool(row and row["paused"])


def _running_job() -> dict | None:
    row = g.conn.execute(
        "SELECT * FROM jobs WHERE status = 'running' LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _queued_jobs() -> list[dict]:
    rows = g.conn.execute(
        "SELECT * FROM jobs WHERE status IN ('queued', 'paused') ORDER BY priority ASC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _recent_jobs() -> list[dict]:
    rows = g.conn.execute(
        """
        SELECT * FROM jobs
        WHERE  status IN ('done','failed','cancelled','interrupted')
          AND  finished_at >= datetime('now', '-24 hours')
        ORDER BY finished_at DESC
        LIMIT  50
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def register(app: Flask) -> None:
    @app.route("/")
    def index():
        running = _running_job()
        if running and running.get("progress_eta") is not None:
            running["eta_fmt"] = _fmt_eta(running["progress_eta"])
        queued = _queued_jobs()
        recent = _recent_jobs()
        worker = _worker_status()
        queue_paused = _queue_paused()
        return render_template(
            "index.html",
            running=running,
            queued=queued,
            recent=recent,
            worker=worker,
            queue_paused=queue_paused,
        )

    @app.route("/jobs/<int:job_id>/pause", methods=["POST"])
    def job_pause(job_id: int):
        g.conn.execute(
            "UPDATE jobs SET status = 'paused' WHERE id = ? AND status = 'queued'",
            (job_id,),
        )
        g.conn.commit()
        return redirect("/", 303)

    @app.route("/jobs/<int:job_id>/resume", methods=["POST"])
    def job_resume(job_id: int):
        g.conn.execute(
            "UPDATE jobs SET status = 'queued' WHERE id = ? AND status = 'paused'",
            (job_id,),
        )
        g.conn.commit()
        return redirect("/", 303)

    @app.route("/jobs/<int:job_id>/retry", methods=["POST"])
    def job_retry(job_id: int):
        g.conn.execute(
            """
            UPDATE jobs
            SET    status        = 'queued',
                   progress_pct  = NULL,
                   progress_fps  = NULL,
                   progress_eta  = NULL,
                   exit_code     = NULL,
                   error_message = NULL,
                   started_at    = NULL,
                   finished_at   = NULL
            WHERE  id = ? AND status IN ('done','failed','cancelled','interrupted')
            """,
            (job_id,),
        )
        g.conn.commit()
        return redirect("/", 303)

    @app.route("/queue/pause", methods=["POST"])
    def queue_pause():
        g.conn.execute("UPDATE queue_settings SET paused = 1 WHERE id = 1")
        g.conn.commit()
        return redirect("/", 303)

    @app.route("/queue/resume", methods=["POST"])
    def queue_resume():
        g.conn.execute("UPDATE queue_settings SET paused = 0 WHERE id = 1")
        g.conn.commit()
        return redirect("/", 303)
