from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from rne import db
from rne.models import HandbrakeArgs, JobStatus


def run(args) -> None:
    conn = db.connect()
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

            recheck = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (args.id,)
            ).fetchone()
            if recheck["status"] == JobStatus.RUNNING:
                print("job is now running; cannot edit", file=sys.stderr)
                sys.exit(1)
            break

        conn.execute(
            "UPDATE jobs SET handbrake_args = ? WHERE id = ?",
            (new_args.to_json(), args.id),
        )
        conn.commit()
        print(f"Job {args.id} updated.")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
