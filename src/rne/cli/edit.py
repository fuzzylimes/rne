from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from rne import db, jobs
from rne.models import HandbrakeArgs, JobStatus


def run(args) -> None:
    if getattr(args, "raw", False):
        if args.id is None:
            print("--raw needs a job ID.", file=sys.stderr)
            sys.exit(2)
        run_raw(args)
    else:
        run_tui(args)


def run_tui(args) -> None:
    """Full-screen editor: one job, a whole batch, or a picker when neither is given."""
    from rne.cli.ingest import tvdb_lookup_or_exit
    from rne.tui.edit import EditApp

    lookup, lookup_note = tvdb_lookup_or_exit()
    conn = db.connect()
    db.init_db(conn)
    if args.batch is not None:
        job_ids = jobs.batch_job_ids(conn, args.batch)
        if not job_ids:
            print(f"Batch {args.batch} has no editable jobs.", file=sys.stderr)
            sys.exit(1)
    elif args.id is not None:
        if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (args.id,)).fetchone() is None:
            print(f"Job {args.id} not found.", file=sys.stderr)
            sys.exit(1)
        job_ids = [args.id]
    else:
        job_ids = None
    # None when the user quits from the picker.
    from rne.tui.common import clear_after_app

    result = EditApp(conn, job_ids, lookup=lookup, lookup_note=lookup_note).run()
    clear_after_app()
    lines, ok = result or ([], True)
    for line in lines:
        print(line)
    if not ok:
        sys.exit(1)


def run_raw(args) -> None:
    """Edit handbrake_args JSON in $EDITOR (the pre-v0.6 behaviour)."""
    conn = db.connect()
    db.init_db(conn)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"Job {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    if row["status"] == JobStatus.RUNNING:
        print(
            f"Job {args.id} is currently running. Stop the worker before editing.",
            file=sys.stderr,
        )
        sys.exit(1)

    current_json = json.loads(HandbrakeArgs.from_json(row["handbrake_args"]).to_json())
    editor = os.environ.get("EDITOR", "vi")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix=f"rne_job{args.id}_"
    ) as f:
        json.dump(current_json, f, indent=2)
        tmp = f.name

    try:
        while True:
            subprocess.run([editor, tmp], check=False)
            try:
                with open(tmp) as f:
                    new_args = HandbrakeArgs.from_json(f.read())
            except Exception as exc:
                print(f"Validation error: {exc}", file=sys.stderr)
                raw = input("Re-open editor? [Y/n]: ").strip().lower()
                if raw not in ("", "y"):
                    print("Edit cancelled.", file=sys.stderr)
                    sys.exit(1)
                continue
            break

        # The worker may have claimed the job while the editor was open;
        # update_job_args refuses atomically in that case.
        try:
            jobs.update_job_args(conn, args.id, new_args)
        except jobs.JobRunning:
            print("job is now running; cannot edit", file=sys.stderr)
            sys.exit(1)
        print(f"Job {args.id} updated.")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
