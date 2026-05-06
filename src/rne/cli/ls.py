from __future__ import annotations

import sqlite3

from rne import db


def _label(row: sqlite3.Row) -> str:
    if row["show"]:
        return f"{row['show']} S{row['season']:02d}E{row['episode']:02d}"
    return row["movie"] or "(unknown)"


def _progress(row: sqlite3.Row) -> str:
    if row["status"] == "running" and row["progress_pct"] is not None:
        return f" {row['progress_pct']:.1f}%"
    return ""


def run(args) -> None:
    conn = db.connect()

    if getattr(args, "status", None):
        statuses = [s.strip() for s in args.status.split(",")]
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY id DESC",
            statuses,
        ).fetchall()
    elif getattr(args, "all", False):
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM jobs
                WHERE  status IN ('queued', 'paused', 'running')
                UNION ALL
                SELECT * FROM jobs
                WHERE  status IN ('done', 'failed', 'cancelled', 'interrupted')
                  AND  finished_at >= datetime('now', '-24 hours')
            )
            ORDER BY id DESC
            """
        ).fetchall()

    if not rows:
        print("No jobs found.")
        return

    print(f"{'ID':>4}  {'Label':<42}  {'Status':<12}  Info")
    print("-" * 72)
    for row in rows:
        label = _label(row)
        status = row["status"]
        extra = _progress(row)
        err = ""
        if status in ("failed", "interrupted") and row["error_message"]:
            snippet = row["error_message"][:60].replace("\n", " ")
            err = f"  [{snippet}]"
        print(f"{row['id']:>4}  {label:<42}  {status:<12}{extra}{err}")
