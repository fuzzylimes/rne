from __future__ import annotations

import sys

from rne import db

_TERMINAL = frozenset({"done", "failed", "cancelled", "interrupted"})
_CANCELLABLE = frozenset({"queued", "paused"})


def run_cancel(args) -> None:
    conn = db.connect()
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"Job {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    if row["status"] not in _CANCELLABLE:
        print(
            f"Job {args.id} is '{row['status']}'; "
            f"only {sorted(_CANCELLABLE)} jobs can be cancelled.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (args.id,))
    conn.commit()
    print(f"Job {args.id} cancelled.")


def run_retry(args) -> None:
    conn = db.connect()
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"Job {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    if row["status"] not in _TERMINAL:
        print(
            f"Job {args.id} is '{row['status']}'; "
            f"retry only works on terminal states: {sorted(_TERMINAL)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn.execute(
        """
        UPDATE jobs
        SET    status        = 'queued',
               attempt_count = attempt_count + 1,
               progress_pct  = NULL,
               progress_fps  = NULL,
               progress_eta  = NULL,
               error_message = NULL,
               exit_code     = NULL,
               started_at    = NULL,
               finished_at   = NULL
        WHERE  id = ?
        """,
        (args.id,),
    )
    conn.commit()
    print(f"Job {args.id} requeued.")


def run_pause(_args) -> None:
    """Pause the global queue (worker stops picking new jobs after current finishes)."""
    conn = db.connect()
    conn.execute("UPDATE queue_settings SET paused = 1 WHERE id = 1")
    conn.commit()
    print("Queue paused. Worker will stop after the current job finishes.")


def run_resume(_args) -> None:
    """Resume the global queue."""
    conn = db.connect()
    conn.execute("UPDATE queue_settings SET paused = 0 WHERE id = 1")
    conn.commit()
    print("Queue resumed.")
