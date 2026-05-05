from rne import db
from rne.models import HandbrakeArgs, Job
from tests.conftest import insert_job


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"jobs", "ingest_batches", "worker_status", "queue_settings"} <= tables


def test_init_db_creates_indexes(conn):
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert {"idx_jobs_status", "idx_jobs_claim", "idx_jobs_batch"} <= indexes


def test_init_db_idempotent(conn):
    # Calling init_db a second time must not raise or duplicate seed rows.
    db.init_db(conn)
    count = conn.execute("SELECT COUNT(*) FROM worker_status").fetchone()[0]
    assert count == 1
    count = conn.execute("SELECT COUNT(*) FROM queue_settings").fetchone()[0]
    assert count == 1


def test_init_db_seeds_worker_status(conn):
    row = conn.execute("SELECT id, state FROM worker_status").fetchone()
    assert row["id"] == 1
    assert row["state"] == "starting"


def test_init_db_seeds_queue_settings(conn):
    row = conn.execute("SELECT id, paused FROM queue_settings").fetchone()
    assert row["id"] == 1
    assert row["paused"] == 0


# ---------------------------------------------------------------------------
# claim_next_job
# ---------------------------------------------------------------------------

def test_claim_next_job_empty_returns_none(conn):
    assert db.claim_next_job(conn) is None


def test_claim_next_job_returns_job(conn):
    job_id = insert_job(conn, movie="Aliens")
    job = db.claim_next_job(conn)
    assert isinstance(job, Job)
    assert job.id == job_id
    assert job.movie == "Aliens"


def test_claim_next_job_sets_status_running(conn):
    insert_job(conn)
    db.claim_next_job(conn)
    row = conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "running"


def test_claim_next_job_increments_attempt_count(conn):
    insert_job(conn)
    job = db.claim_next_job(conn)
    assert job.attempt_count == 1


def test_claim_next_job_clears_progress_fields(conn):
    # Pre-populate progress as if from a previous attempt.
    job_id = insert_job(conn)
    conn.execute(
        "UPDATE jobs SET progress_pct=50, progress_fps=30, progress_eta=120 WHERE id=?",
        (job_id,),
    )
    conn.commit()
    job = db.claim_next_job(conn)
    assert job.progress_pct is None
    assert job.progress_fps is None
    assert job.progress_eta is None


def test_claim_next_job_sets_started_at(conn):
    insert_job(conn)
    job = db.claim_next_job(conn)
    assert job.started_at is not None


def test_claim_next_job_handbrake_args_deserialized(conn):
    insert_job(conn)
    job = db.claim_next_job(conn)
    assert isinstance(job.handbrake_args, HandbrakeArgs)
    assert job.handbrake_args.encoder == "x265"


def test_claim_next_job_skips_paused(conn):
    insert_job(conn, status="paused")
    assert db.claim_next_job(conn) is None


def test_claim_next_job_skips_running(conn):
    insert_job(conn, status="running")
    assert db.claim_next_job(conn) is None


def test_claim_next_job_skips_terminal_states(conn):
    for status in ("done", "failed", "cancelled", "interrupted"):
        insert_job(conn, status=status, source_path=f"/s/{status}.mkv",
                   output_path=f"/o/{status}.mkv")
    assert db.claim_next_job(conn) is None


def test_claim_next_job_priority_order(conn):
    # Lower priority value runs first.
    insert_job(conn, priority=10, source_path="/s/low.mkv", output_path="/o/low.mkv")
    id_high = insert_job(conn, priority=0, source_path="/s/high.mkv", output_path="/o/high.mkv")
    job = db.claim_next_job(conn)
    assert job.id == id_high


def test_claim_next_job_id_order_on_priority_tie(conn):
    # Same priority → lower id (insertion order) wins.
    id_first = insert_job(conn, priority=0, source_path="/s/first.mkv", output_path="/o/first.mkv")
    insert_job(conn, priority=0, source_path="/s/second.mkv", output_path="/o/second.mkv")
    job = db.claim_next_job(conn)
    assert job.id == id_first


def test_claim_next_job_only_claims_one(conn):
    insert_job(conn, source_path="/s/a.mkv", output_path="/o/a.mkv")
    insert_job(conn, source_path="/s/b.mkv", output_path="/o/b.mkv")
    db.claim_next_job(conn)
    queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
    assert queued == 1
    assert running == 1


def test_claim_next_job_second_call_claims_next(conn):
    id1 = insert_job(conn, priority=0, source_path="/s/a.mkv", output_path="/o/a.mkv")
    id2 = insert_job(conn, priority=0, source_path="/s/b.mkv", output_path="/o/b.mkv")
    j1 = db.claim_next_job(conn)
    # claim only touches 'queued' rows, so j2 is still claimable while j1 is running
    j2 = db.claim_next_job(conn)
    assert j1.id == id1
    assert j2.id == id2


# ---------------------------------------------------------------------------
# reconcile_orphans
# ---------------------------------------------------------------------------

def test_reconcile_orphans_flips_running_to_interrupted(conn):
    job_id = insert_job(conn, status="running")
    count = db.reconcile_orphans(conn)
    assert count == 1
    row = conn.execute("SELECT status, error_message FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "interrupted"
    assert row["error_message"] is not None


def test_reconcile_orphans_sets_finished_at(conn):
    job_id = insert_job(conn, status="running")
    db.reconcile_orphans(conn)
    row = conn.execute("SELECT finished_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["finished_at"] is not None


def test_reconcile_orphans_leaves_other_statuses_untouched(conn):
    for status in ("queued", "paused", "done", "failed", "cancelled", "interrupted"):
        insert_job(conn, status=status, source_path=f"/s/{status}.mkv",
                   output_path=f"/o/{status}.mkv")
    db.reconcile_orphans(conn)
    for status in ("queued", "paused", "done", "failed", "cancelled", "interrupted"):
        row = conn.execute(
            "SELECT status FROM jobs WHERE source_path=?", (f"/s/{status}.mkv",)
        ).fetchone()
        assert row["status"] == status


def test_reconcile_orphans_returns_zero_when_none_running(conn):
    insert_job(conn, status="queued")
    assert db.reconcile_orphans(conn) == 0


def test_reconcile_orphans_handles_multiple_running(conn):
    insert_job(conn, status="running", source_path="/s/a.mkv", output_path="/o/a.mkv")
    insert_job(conn, status="running", source_path="/s/b.mkv", output_path="/o/b.mkv")
    count = db.reconcile_orphans(conn)
    assert count == 2
    rows = conn.execute("SELECT status FROM jobs WHERE status='interrupted'").fetchall()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# HandbrakeArgs round-trip
# ---------------------------------------------------------------------------

def test_handbrake_args_roundtrip_defaults(conn):
    original = HandbrakeArgs()
    restored = HandbrakeArgs.from_json(original.to_json())
    assert restored == original


def test_handbrake_args_roundtrip_custom_values(conn):
    from rne.models import AudioTrack
    original = HandbrakeArgs(
        encoder="x264",
        quality=22,
        preset="medium",
        audio_tracks=[AudioTrack(track=1, codec="copy"), AudioTrack(track=3, codec="ac3", bitrate=640)],
        subtitle_tracks=[2],
        decomb=True,
        extra_args=["--no-dvdnav"],
    )
    restored = HandbrakeArgs.from_json(original.to_json())
    assert restored == original


def test_handbrake_args_roundtrip_empty_lists(conn):
    original = HandbrakeArgs(audio_tracks=[], subtitle_tracks=[], extra_args=[])
    restored = HandbrakeArgs.from_json(original.to_json())
    assert restored.audio_tracks == []
    assert restored.subtitle_tracks == []
    assert restored.extra_args == []
