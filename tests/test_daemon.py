from __future__ import annotations

import sqlite3

import pytest

from rne import db
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
        row = mem_conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "interrupted"


def test_reconcile_sets_error_message(mem_conn):
    job_id = insert_job(mem_conn, status="running")
    db.reconcile_orphans(mem_conn)
    row = mem_conn.execute("SELECT error_message FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["error_message"] is not None
    assert len(row["error_message"]) > 0


def test_reconcile_sets_finished_at(mem_conn):
    job_id = insert_job(mem_conn, status="running")
    db.reconcile_orphans(mem_conn)
    row = mem_conn.execute("SELECT finished_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["finished_at"] is not None
