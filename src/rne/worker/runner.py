from __future__ import annotations

import collections
import contextvars
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from rne import db, handbrake

if TYPE_CHECKING:
    import sqlite3
    from rne.models import Job

# Shared with daemon.py's SIGTERM handler so it can forward the signal.
current_proc: contextvars.ContextVar[subprocess.Popen | None] = contextvars.ContextVar(
    "current_proc", default=None
)

_PROGRESS_RE = re.compile(
    r"Encoding: task \d+ of \d+,\s*"
    r"(?P<pct>[\d.]+)\s*%\s*"
    r"\(\s*(?P<fps>[\d.]+)\s*fps,\s*avg\s*[\d.]+\s*fps,\s*ETA\s*"
    r"(?P<eta_h>\d+)h(?P<eta_m>\d+)m(?P<eta_s>\d+)s\s*\)"
)


def parse_handbrake_progress(line: str) -> dict | None:
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    eta_seconds = (
        int(m.group("eta_h")) * 3600
        + int(m.group("eta_m")) * 60
        + int(m.group("eta_s"))
    )
    return {
        "pct": float(m.group("pct")),
        "fps": float(m.group("fps")),
        "eta": eta_seconds,
    }


def _drain_to_deque(pipe, buf: collections.deque) -> None:
    for line in pipe:
        buf.append(line.rstrip("\n"))
        print(line, end="", file=sys.stderr)


def run_job(job: "Job", conn: "sqlite3.Connection") -> None:
    partial = pathlib.Path(job.output_path + ".partial")
    partial.unlink(missing_ok=True)
    partial.parent.mkdir(parents=True, exist_ok=True)

    cmd = handbrake.build_command(
        job.source_path,
        str(partial),
        job.handbrake_args,
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    current_proc.set(proc)

    last_update = 0.0
    stderr_buf: collections.deque[str] = collections.deque(maxlen=200)

    t = threading.Thread(
        target=_drain_to_deque, args=(proc.stderr, stderr_buf), daemon=True
    )
    t.start()

    for line in proc.stdout:
        progress = parse_handbrake_progress(line)
        if progress and time.monotonic() - last_update > 20:
            db.update_progress(conn, job.id, **progress)
            last_update = time.monotonic()

    proc.wait()
    t.join(timeout=2)

    current_proc.set(None)

    if proc.returncode == 0:
        os.rename(str(partial), job.output_path)
        db.mark_done(conn, job.id)
    else:
        error_msg = "\n".join(stderr_buf)[-500:]
        db.mark_failed(conn, job.id, proc.returncode, error_message=error_msg)
