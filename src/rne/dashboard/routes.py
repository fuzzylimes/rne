from __future__ import annotations

import datetime
import hmac
import pathlib
import shlex
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
)

from rne import config, db, handbrake, jobs, naming, plan
from rne.dashboard.forms import EditForm
from rne.models import HandbrakeArgs, JobStatus

TERMINAL_STATUSES = ("done", "failed", "cancelled", "interrupted")
WAITING_STATUSES = ("queued", "paused")


# ---------------------------------------------------------------------------
# Page data
# ---------------------------------------------------------------------------


def _worker_status() -> dict:
    row = g.conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()
    if row is None:
        return {
            "state": "unknown",
            "last_seen": None,
            "indicator": "offline",
            "ago": None,
        }

    last_seen_str = row["last_seen"]
    try:
        last_seen = datetime.datetime.fromisoformat(last_seen_str)
        # SQLite's CURRENT_TIMESTAMP is UTC without a zone.
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        age = (now - last_seen).total_seconds()
    except (TypeError, ValueError):
        age = None

    if age is None or age > 120:
        indicator = "offline"
    elif age > 90:
        indicator = "red"
    elif age > 30:
        indicator = "amber"
    else:
        indicator = "green"

    return {
        "state": row["state"],
        "last_seen": last_seen_str,
        "indicator": indicator,
        "ago": int(age) if age is not None else None,
        "current_job_id": row["current_job_id"],
    }


def _queue_paused() -> bool:
    row = g.conn.execute("SELECT paused FROM queue_settings WHERE id = 1").fetchone()
    return bool(row and row["paused"])


_JOB_SELECT = """
    SELECT j.*, b.label AS batch_label
    FROM   jobs j LEFT JOIN ingest_batches b ON b.id = j.ingest_batch_id
"""


def _settings_line(args_json: str) -> str:
    """e.g. 'RF 20 · slow · 2 audio · 1 sub'."""
    try:
        args = HandbrakeArgs.from_json(args_json)
    except (ValueError, TypeError, KeyError):
        return ""
    parts = [f"RF {args.quality}", args.preset]
    if args.tune:
        parts.append(args.tune)
    parts.append(f"{len(args.audio_tracks)} audio")
    if args.subtitle_tracks:
        parts.append(f"{len(args.subtitle_tracks)} sub")
    if args.chapter_start is not None:
        parts.append(f"ch {args.chapter_start}–{args.chapter_end}")
    return " · ".join(parts)


def _view(row) -> dict:
    """A job row plus the display fields every template uses."""
    job = dict(row)
    is_tv = job["show"] is not None
    job["is_tv"] = is_tv
    job["label"] = (
        naming.episode_label(job["season"], job["episode"]) if is_tv else "Movie"
    )
    job["name"] = naming.folder_name(job["show"] or job["movie"], job.get("year"))
    job["settings"] = _settings_line(job["handbrake_args"])
    if job.get("progress_eta") is not None:
        job["eta_fmt"] = _fmt_eta(job["progress_eta"])
    return job


def _running_job() -> dict | None:
    row = g.conn.execute(_JOB_SELECT + " WHERE j.status = 'running' LIMIT 1").fetchone()
    return _view(row) if row else None


def _queued_jobs() -> list[dict]:
    rows = g.conn.execute(
        _JOB_SELECT + " WHERE j.status IN ('queued', 'paused') ORDER BY j.priority, j.id"
    ).fetchall()
    return [_view(r) for r in rows]


def _recent_jobs() -> list[dict]:
    rows = g.conn.execute(
        _JOB_SELECT
        + """
        WHERE  j.status IN ('done','failed','cancelled','interrupted')
          AND  j.finished_at >= datetime('now', '-24 hours')
        ORDER BY j.finished_at DESC
        LIMIT  50
        """
    ).fetchall()
    return [_view(r) for r in rows]


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _live_context() -> dict:
    return {
        "running": _running_job(),
        "queued": _queued_jobs(),
        "recent": _recent_jobs(),
        "worker": _worker_status(),
        "queue_paused": _queue_paused(),
    }


def _job_or_404(job_id: int):
    row = g.conn.execute(_JOB_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _command_preview(row, args: HandbrakeArgs) -> tuple[str | None, str | None]:
    """(command, None) or (None, why it can't be built). Mirrors the worker's call."""
    try:
        cmd = handbrake.build_command(row["source_path"], row["output_path"] + ".partial", args)
    except ValueError as exc:
        return None, str(exc)
    return shlex.join(cmd), None


def _library_config() -> config.LibraryConfig:
    try:
        return config.load_library_config()
    except config.ConfigError as exc:
        flash(f"Config error: {exc}", "error")
        return config.LibraryConfig()


def _tvdb_enabled() -> tuple[bool, str | None]:
    try:
        cfg, note = config.load_tvdb_config()
    except config.ConfigError as exc:
        return False, f"config error: {exc}"
    return cfg is not None, note


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _safe_next(default: str = "/") -> str:
    """Where a POST returns to: a local path from the form, or the default."""
    target = request.form.get("next") or request.args.get("next") or default
    if not target.startswith("/") or target.startswith("//"):
        return default
    return target


def _done(message: str | None = None, category: str = "ok"):
    """Finish a POST: 204 for the page's own fetch, else redirect back (PRG)."""
    if request.headers.get("X-Rne-Async"):
        return "", 204
    if message:
        flash(message, category)
    return redirect(_safe_next(), 303)


def _form_ids(name: str = "ids") -> list[int]:
    out = []
    for raw in request.form.getlist(name):
        if raw.strip().isdigit():
            out.append(int(raw))
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register(app: Flask) -> None:
    @app.route("/")
    def index():
        return render_template("index.html", **_live_context())

    @app.route("/partials/live")
    def live_partial():
        # The index polls this and swaps it in place, so drags and taps aren't lost.
        return render_template("_live.html", **_live_context())

    # -- one job ------------------------------------------------------------------

    @app.route("/jobs/<int:job_id>")
    def job_detail(job_id: int):
        row = _job_or_404(job_id)
        target = jobs.load_edit_targets(g.conn, [job_id])[0]
        command, command_error = _command_preview(row, target.args)
        batch_count = 0
        if row["ingest_batch_id"] is not None:
            batch_count = g.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE ingest_batch_id = ?", (row["ingest_batch_id"],)
            ).fetchone()[0]
        waiting = jobs.waiting_job_ids(g.conn)
        position = waiting.index(job_id) + 1 if job_id in waiting else None
        # "Run this batch next" only when it would change something.
        batch_waiting = (jobs.batch_waiting_ids(g.conn, row["ingest_batch_id"])
                         if row["ingest_batch_id"] is not None else [])
        batch_can_jump = len(batch_waiting) > 1 and waiting[:len(batch_waiting)] != batch_waiting
        return render_template(
            "job.html",
            job=_view(row),
            target=target,
            audio=plan.audio_rows(target.summary, target.args),
            subtitles=plan.subtitle_rows(target.summary, target.args),
            command=command,
            command_error=command_error,
            batch_count=batch_count,
            position=position,
            queue_length=len(waiting),
            batch_can_jump=batch_can_jump,
        )

    @app.route("/jobs/<int:job_id>/pause", methods=["POST"])
    def job_pause(job_id: int):
        g.conn.execute(
            "UPDATE jobs SET status = 'paused' WHERE id = ? AND status = 'queued'",
            (job_id,),
        )
        g.conn.commit()
        return _done()

    @app.route("/jobs/<int:job_id>/resume", methods=["POST"])
    def job_resume(job_id: int):
        g.conn.execute(
            "UPDATE jobs SET status = 'queued' WHERE id = ? AND status = 'paused'",
            (job_id,),
        )
        g.conn.commit()
        return _done()

    @app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
    def job_cancel(job_id: int):
        cur = g.conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status IN ('queued','paused')",
            (job_id,),
        )
        g.conn.commit()
        if cur.rowcount == 0:
            return _done(f"Job {job_id} could not be cancelled (it may have started).", "error")
        return _done(f"Job {job_id} cancelled.")

    @app.route("/jobs/<int:job_id>/retry", methods=["POST"])
    def job_retry(job_id: int):
        g.conn.execute(
            """
            UPDATE jobs
            SET    status        = 'queued',
                   progress_pct  = NULL,
                   progress_fps  = NULL,
                   progress_eta  = NULL,
                   exit_code     = NULL,
                   error_message = NULL,
                   started_at    = NULL,
                   finished_at   = NULL
            WHERE  id = ? AND status IN ('done','failed','cancelled','interrupted')
              AND  (move_state IS NULL OR move_state != 'moving')
            """,
            (job_id,),
        )
        g.conn.commit()
        return _done()

    @app.route("/jobs/<int:job_id>/move", methods=["POST"])
    def job_move(job_id: int):
        # The worker's mover thread picks it up within a few seconds.
        db.request_move(g.conn, job_id)
        return _done()

    @app.route("/jobs/<int:job_id>/top", methods=["POST"])
    def job_top(job_id: int):
        jobs.move_to_top(g.conn, [job_id])
        return _done(f"Job {job_id} runs next.")

    @app.route("/batches/<int:batch_id>/top", methods=["POST"])
    def batch_top(batch_id: int):
        ids = jobs.batch_waiting_ids(g.conn, batch_id)
        jobs.move_to_top(g.conn, ids)
        return _done(f"{len(ids)} job(s) from this batch run next.")

    @app.route("/queue/order", methods=["POST"])
    def queue_order():
        jobs.reorder(g.conn, _form_ids())
        return "", 204

    @app.route("/queue/pause", methods=["POST"])
    def queue_pause():
        g.conn.execute("UPDATE queue_settings SET paused = 1 WHERE id = 1")
        g.conn.commit()
        return _done()

    @app.route("/queue/resume", methods=["POST"])
    def queue_resume():
        g.conn.execute("UPDATE queue_settings SET paused = 0 WHERE id = 1")
        g.conn.commit()
        return _done()

    # -- editing ------------------------------------------------------------------

    def _edit(job_ids: list[int], *, action: str, cancel_url: str, heading: str):
        if not job_ids:
            flash("Nothing to edit: no job in this batch is waiting or finished.", "error")
            return redirect(cancel_url, 303)
        targets = jobs.load_edit_targets(g.conn, job_ids)
        running = [t for t in targets if t.status == JobStatus.RUNNING]
        if request.method == "GET" and running:
            flash(f"Job {running[0].job_id} is encoding right now, so it can't be "
                  "edited. Stop the worker first, or wait for it to finish.", "error")
            return redirect(cancel_url, 303)
        if len({t.is_tv for t in targets}) > 1:
            flash("This batch mixes TV and movie jobs; edit them one at a time.", "error")
            return redirect(cancel_url, 303)

        if request.method == "GET":
            form = EditForm.from_targets(targets, _library_config())
            results, errors = form.plan()
            status = 200
        else:
            form = EditForm.from_form(targets, request.form)
            results, errors = form.plan()
            if request.headers.get("X-Rne-Preview"):
                return render_template("_edit_preview.html", results=results, errors=errors)
            if not errors:
                lines, ok = jobs.apply_edits(g.conn, results)
                for line in lines:
                    flash(line, "ok" if ok else "error")
                return redirect(cancel_url, 303)
            status = 422
        tvdb_on, tvdb_note = _tvdb_enabled()
        return render_template(
            "edit.html", form=form, results=results, errors=errors, action=action,
            cancel_url=cancel_url, heading=heading, tvdb_on=tvdb_on, tvdb_note=tvdb_note,
            submitted=request.method == "POST",
        ), status

    @app.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
    def job_edit(job_id: int):
        row = _job_or_404(job_id)
        job = _view(row)
        return _edit([job_id], action=f"/jobs/{job_id}/edit", cancel_url=f"/jobs/{job_id}",
                     heading=f"Edit job {job_id}: {job['label']} · {job['name']}")

    @app.route("/batches/<int:batch_id>/edit", methods=["GET", "POST"])
    def batch_edit(batch_id: int):
        batch = g.conn.execute(
            "SELECT * FROM ingest_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            abort(404)
        back = _safe_next("/")
        if request.method == "POST":
            # The jobs the form was drawn for, so a job that started meanwhile
            # keeps its place in the episode numbering (and is reported unchanged).
            in_batch = {r[0] for r in g.conn.execute(
                "SELECT id FROM jobs WHERE ingest_batch_id = ?", (batch_id,))}
            ids = [i for i in _form_ids("job") if i in in_batch]
        else:
            ids = jobs.batch_job_ids(g.conn, batch_id)
        return _edit(ids, action=f"/batches/{batch_id}/edit?next={quote(back)}", cancel_url=back,
                     heading=f"Edit batch: {batch['label']}")

    # -- recordings API -------------------------------------------------------------

    @app.route("/api/v1/recordings", methods=["POST"])
    def api_recordings():
        """Queue a finished Jellyfin Live TV recording. Form or JSON: path=..."""
        from rne import recordings

        try:
            api = config.load_api_config()
        except config.ConfigError as exc:
            return jsonify(error=f"config error: {exc}"), 500
        if api is None:
            return jsonify(error="the recordings API is off: add an [api] table to the config"), 404
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(token.encode(), api.token.encode()):
            return jsonify(error="missing or wrong token"), 401

        body = request.get_json(silent=True) if request.is_json else None
        raw_path = (body or {}).get("path") if body else request.form.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return jsonify(error="path is required"), 400
        try:
            roots = config.resolve_roots()
            outcome = recordings.enqueue(
                g.conn, pathlib.Path(raw_path.strip()), recordings_root=api.recordings_root,
                staging_root=roots.staging_root, library=config.load_library_config(),
            )
        except config.ConfigError as exc:
            return jsonify(error=f"config error: {exc}"), 500
        except recordings.RecordingError as exc:
            return jsonify(error=str(exc)), 422
        app.logger.info("recording %s: %s %s", raw_path, outcome.status,
                        outcome.reason or outcome.job_id)
        payload = {k: v for k, v in vars(outcome).items() if v not in (None, "")}
        return jsonify(payload), 201 if outcome.status == "queued" else 200

    @app.route("/tvdb/search")
    def tvdb_search():
        from rne import tvdb
        from rne.cli._pipeline import mungefilename

        query = (request.args.get("q") or "").strip()
        kind = "movie" if request.args.get("kind") == "movie" else "series"
        matches, error = [], None
        if not query:
            error = "Type a name to search for."
        else:
            try:
                lookup, note = tvdb.lookup_from_config()
                if lookup is None:
                    error = f"TVDB lookup is off: {note}"
                else:
                    matches = lookup(query, kind)
            except config.ConfigError as exc:
                error = f"Config error: {exc}"
            except tvdb.TvdbError as exc:
                error = str(exc)
        return render_template(
            "_tvdb_results.html", matches=matches, error=error, query=query,
            clean=mungefilename,
        )
