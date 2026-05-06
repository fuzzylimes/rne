from __future__ import annotations

import sqlite3
import time

import pytest

from rne import db
from rne.worker import heartbeat
from tests.conftest import insert_job


@pytest.fixture
def mem_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# reconcile_orphans
# ---------------------------------------------------------------------------


def test_reconcile_orphans_flips_running_to_interrupted(mem_conn):
    job_id = insert_job(mem_conn, status="running")

    count = db.reconcile_orphans(mem_conn)

    assert count == 1
    row = mem_conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "interrupted"


def test_reconcile_orphans_ignores_other_states(mem_conn):
    insert_job(mem_conn, status="queued")
    insert_job(mem_conn, status="done")
    insert_job(mem_conn, status="failed")

    count = db.reconcile_orphans(mem_conn)

    assert count == 0


def test_reconcile_orphans_multiple_running(mem_conn):
    id1 = insert_job(mem_conn, status="running", movie="Movie A")
    id2 = insert_job(mem_conn, status="running", movie="Movie B")

    count = db.reconcile_orphans(mem_conn)

    assert count == 2
    for job_id in (id1, id2):
        row = mem_conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "interrupted"


def test_reconcile_sets_error_message(mem_conn):
    job_id = insert_job(mem_conn, status="running")
    db.reconcile_orphans(mem_conn)
    row = mem_conn.execute(
        "SELECT error_message FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["error_message"] is not None
    assert len(row["error_message"]) > 0


def test_reconcile_sets_finished_at(mem_conn):
    job_id = insert_job(mem_conn, status="running")
    db.reconcile_orphans(mem_conn)
    row = mem_conn.execute(
        "SELECT finished_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# heartbeat thread opens its own connection (case 2)
# ---------------------------------------------------------------------------


def test_heartbeat_uses_own_connection(tmp_path):
    """Heartbeat writes last_seen via its own connection, not the caller's."""
    db_path = str(tmp_path / "hb.db")
    setup = db.connect(db_path)
    db.init_db(setup)
    # Stamp last_seen far in the past so any update is detectable.
    setup.execute("UPDATE worker_status SET last_seen = '2000-01-01' WHERE id = 1")
    setup.commit()
    setup.close()

    heartbeat.set_state("idle")
    heartbeat.start_heartbeat_thread(db_path=db_path)
    time.sleep(0.5)  # heartbeat writes immediately before its first sleep

    verify = db.connect(db_path)
    row = verify.execute("SELECT last_seen, state FROM worker_status WHERE id=1").fetchone()
    verify.close()

    assert row["last_seen"] != "2000-01-01", "heartbeat must have updated last_seen"
    assert row["state"] == "idle"
