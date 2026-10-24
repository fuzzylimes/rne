"""Move finished encodes from staging into the library.

Runs on its own thread inside the worker, so a slow copy to another disk
never holds up the next encode. The worker is the only process that moves
files: `rne move` and the dashboard just mark a job's move as pending.

Raw rips are never touched; only the encoded file moves.
"""

from __future__ import annotations

import errno
import os
import pathlib
import shutil
import sys
import threading

from rne import db

POLL_SECONDS = 5


def move_file(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Move src to dst, replacing dst. Never leaves a half-written dst.

    Same filesystem: one atomic rename. Across filesystems: copy to
    "dst.partial", flush to disk, rename over dst, then delete src.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    partial = dst.with_name(dst.name + ".partial")
    try:
        shutil.copy2(src, partial)
        with open(partial, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(partial, dst)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    src.unlink()


def move_one(conn) -> bool:
    """Do the oldest pending move, if any. Returns False when there was none."""
    row = db.claim_next_move(conn)
    if row is None:
        return False
    src, dst = pathlib.Path(row["output_path"]), pathlib.Path(row["final_path"])
    try:
        if src.exists():
            move_file(src, dst)
        elif not dst.exists():
            raise FileNotFoundError(f"encoded file not found: {src}")
        # src gone but dst present: an earlier attempt finished the move
        # before it could be recorded.
    except Exception as exc:  # noqa: BLE001 - recorded on the job for the user
        print(f"move failed for job {row['id']}: {exc}", file=sys.stderr)
        db.mark_move_failed(conn, row["id"], str(exc))
        return True
    print(f"job {row['id']}: moved to {dst}", file=sys.stderr)
    db.mark_moved(conn, row["id"])
    return True


def _loop(stop: threading.Event, db_path: str | None) -> None:
    conn = db.connect(db_path)  # own connection: sqlite3 objects are per-thread
    while not stop.is_set():
        try:
            while move_one(conn) and not stop.is_set():
                pass
        except Exception as exc:  # noqa: BLE001 - keep the mover alive
            print(f"mover error: {exc}", file=sys.stderr)
        stop.wait(POLL_SECONDS)


def start_mover_thread(stop: threading.Event, db_path: str | None = None) -> threading.Thread:
    t = threading.Thread(target=_loop, args=(stop, db_path), daemon=True, name="mover")
    t.start()
    return t

