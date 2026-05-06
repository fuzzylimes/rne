from __future__ import annotations

import signal
import threading
import time

from rne import db
from rne.worker import heartbeat
from rne.worker.runner import current_proc, run_job

_shutdown = threading.Event()


def setup_signal_handlers() -> None:
    def handle(signum, _frame):
        _shutdown.set()
        heartbeat.set_state("stopping")
        proc = current_proc.get()
        if proc and proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def _queue_paused(conn) -> bool:
    row = conn.execute("SELECT paused FROM queue_settings WHERE id = 1").fetchone()
    return bool(row and row["paused"])


def main() -> None:
    setup_signal_handlers()

    conn = db.connect()
    db.init_db(conn)
    db.reconcile_orphans(conn)

    heartbeat.set_state("idle")
    heartbeat.start_heartbeat_thread(conn)

    while not _shutdown.is_set():
        if _queue_paused(conn):
            time.sleep(5)
            continue

        job = db.claim_next_job(conn)
        if job is None:
            time.sleep(5)
            continue

        heartbeat.set_state("encoding", job.id)
        run_job(job, conn)
        heartbeat.set_state("idle")
