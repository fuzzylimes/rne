from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

_state: str = "starting"
_current_job_id: int | None = None
_lock = threading.Lock()


def set_state(state: str, job_id: int | None = None) -> None:
    global _state, _current_job_id
    with _lock:
        _state = state
        _current_job_id = job_id


def _heartbeat_loop(conn: "sqlite3.Connection") -> None:
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


def start_heartbeat_thread(conn: "sqlite3.Connection") -> threading.Thread:
    t = threading.Thread(target=_heartbeat_loop, args=(conn,), daemon=True)
    t.start()
    return t
