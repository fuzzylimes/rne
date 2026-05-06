from __future__ import annotations

import os
import threading
import time

from rne import db

_state: str = "starting"
_current_job_id: int | None = None
_lock = threading.Lock()


def set_state(state: str, job_id: int | None = None) -> None:
    global _state, _current_job_id
    with _lock:
        _state = state
        _current_job_id = job_id


def _heartbeat_loop(db_path: str | None) -> None:
    # Own connection — sqlite3 connections are not thread-safe; sharing the
    # main thread's connection would cause data corruption under concurrent use.
    conn = db.connect(db_path)
    while True:
        with _lock:
            state = _state
            job_id = _current_job_id
        conn.execute(
            """
            UPDATE worker_status
            SET last_seen = CURRENT_TIMESTAMP, state = ?, current_job_id = ?, pid = ?
            WHERE id = 1
            """,
            (state, job_id, os.getpid()),
        )
        conn.commit()
        time.sleep(10)


def start_heartbeat_thread(db_path: str | None = None) -> threading.Thread:
    t = threading.Thread(target=_heartbeat_loop, args=(db_path,), daemon=True)
    t.start()
    return t
