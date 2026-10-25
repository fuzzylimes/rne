from __future__ import annotations

import pathlib
import sys

from rne import db, naming

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
    cur = conn.execute(
        "UPDATE jobs SET status = 'cancelled' WHERE id = ? AND status IN ('queued','paused')",
        (args.id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        print(
            f"Job {args.id} could not be cancelled (may have started running).",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Job {args.id} cancelled.")


def run_retry(args) -> None:
    conn = db.connect()
    db.init_db(conn)
    row = conn.execute(
        "SELECT status, move_state FROM jobs WHERE id = ?", (args.id,)
    ).fetchone()
    if row is None:
        print(f"Job {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    if row["move_state"] == "moving":
        print(f"Job {args.id} is being moved to the library; retry once that finishes.",
              file=sys.stderr)
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
               progress_pct  = NULL,
               progress_fps  = NULL,
               progress_eta  = NULL,
               error_message = NULL,
               exit_code     = NULL,
               started_at    = NULL,
               finished_at   = NULL
        WHERE  id = ? AND (move_state IS NULL OR move_state != 'moving')
        """,
        (args.id,),
    )
    conn.commit()
    print(f"Job {args.id} requeued.")


def run_move(args) -> None:
    """Queue a library move for a done job: retry a failed one, or move one now."""
    conn = db.connect()
    db.init_db(conn)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        print(f"Job {args.id} not found.", file=sys.stderr)
        sys.exit(1)
    if row["status"] != "done":
        print(f"Job {args.id} is '{row['status']}'; only finished (done) jobs can be moved. "
              "To choose where a queued job goes, use `rne edit`.", file=sys.stderr)
        sys.exit(1)
    if row["move_state"] == "moving":
        print(f"Job {args.id} is already being moved.", file=sys.stderr)
        sys.exit(1)

    is_tv = row["show"] is not None
    final: pathlib.Path | None = None
    if args.to:
        folder = pathlib.Path(args.to).expanduser()
        if not folder.is_absolute():
            print("--to needs an absolute path.", file=sys.stderr)
            sys.exit(2)
        final = naming.library_path(folder, row["output_path"], is_tv=is_tv)
    elif row["final_path"] is None:
        from rne.cli.ingest import library_or_exit

        root = library_or_exit().root_for(is_tv)
        if root is None:
            kind = "tv_root" if is_tv else "movies_root"
            print(f"Job {args.id} has no destination. Pass --to FOLDER, or set "
                  f"[library] {kind} in the config.", file=sys.stderr)
            sys.exit(1)
        title = naming.folder_name(row["show"] or row["movie"], row["year"])
        final = naming.library_path(root / title, row["output_path"], is_tv=is_tv)

    if not db.request_move(conn, args.id, str(final) if final else None):
        print(f"Job {args.id} could not be queued for a move.", file=sys.stderr)
        sys.exit(1)
    target = final or row["final_path"]
    print(f"Job {args.id}: move to {target} queued. The worker does it within a few "
          "seconds (it has to be running); `rne ls` shows the result.")


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


def run_add_recording(args, roots) -> None:
    """Queue Live TV recordings by path; the same rules as POST /api/v1/recordings."""
    from rne import config, recordings
    from rne.cli.ingest import library_or_exit

    if args.root:
        root = pathlib.Path(args.root).expanduser()
    else:
        try:
            api = config.load_api_config()
        except config.ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            sys.exit(2)
        if api is None:
            print("No recordings folder: pass --root, or set [api] recordings_root "
                  "in the config.", file=sys.stderr)
            sys.exit(2)
        root = api.recordings_root
    if not root.is_absolute():
        print("--root needs an absolute path.", file=sys.stderr)
        sys.exit(2)
    library = library_or_exit()
    conn = db.connect()
    db.init_db(conn)
    failed = False
    for raw in args.paths:
        path = pathlib.Path(raw).expanduser().absolute()
        try:
            out = recordings.enqueue(conn, path, recordings_root=root,
                                     staging_root=roots.staging_root, library=library)
        except recordings.RecordingError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            failed = True
            continue
        if out.status == "queued":
            print(f"{path.name}: queued as job {out.job_id} ({out.label}).")
        else:
            print(f"{path.name}: {out.status}, {out.reason}.")
    if failed:
        sys.exit(1)
