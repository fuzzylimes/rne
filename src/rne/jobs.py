"""Job-level operations shared by the CLI, TUI, dashboard and API.

db.py owns the schema and the worker's queries; this module owns the
operations front ends perform on jobs: creating batches, inserting a job plan,
editing settings safely, and loading a job's source track layout.
"""

from __future__ import annotations

import pathlib
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from rne import naming, plan, probe
from rne.models import HandbrakeArgs, JobStatus


class JobNotFound(LookupError):
    pass


class JobRunning(RuntimeError):
    """The worker owns this job; its settings cannot change until it finishes."""


def create_batch_row(
    conn: sqlite3.Connection,
    *,
    is_tv: bool,
    show: str | None,
    movie: str | None,
    season: int | None,
    notes: str,
    label_suffix: str = "",
    year: int | None = None,
    tvdb_id: int | None = None,
) -> int:
    """Insert an ingest_batches row and return its id."""
    label = batch_label(is_tv=is_tv, show=show, movie=movie, season=season, year=year)
    cur = conn.execute(
        "INSERT INTO ingest_batches (label, show, movie, notes, year, tvdb_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (label + label_suffix, show, movie, notes, year, tvdb_id),
    )
    conn.commit()
    return cur.lastrowid


def batch_label(
    *, is_tv: bool, show: str | None, movie: str | None, season: int | None,
    year: int | None = None,
) -> str:
    """'Show (2025) S01' / 'Movie (2024)'."""
    if is_tv:
        return f"{naming.folder_name(str(show), year)} S{season:02d}"
    return naming.folder_name(str(movie), year)


def insert_jobs(conn: sqlite3.Connection, batch_id: int, jobs_plan: list[dict]) -> None:
    """Insert job rows for an already-created ingest batch.

    Creates each output directory. Stores the source probe snapshot when the
    plan entry carries one (source_summary / source_chapters).
    """
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
        pathlib.Path(job["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        summary = job.get("source_summary")
        snapshot = (
            probe.snapshot_to_json(summary, job.get("source_chapters"))
            if summary is not None
            else None
        )
        conn.execute(
            """
            INSERT INTO jobs
              (show, season, episode, movie, source_path, output_path,
               handbrake_args, ingest_batch_id, source_probe, year, tvdb_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["show"],
                job["season"],
                job["episode"],
                job["movie"],
                job["source_path"],
                job["output_path"],
                hb.to_json(),
                batch_id,
                snapshot,
                job.get("year"),
                job.get("tvdb_id"),
            ),
        )
    conn.commit()


def update_job_args(conn: sqlite3.Connection, job_id: int, args: HandbrakeArgs) -> None:
    """Replace a job's encode settings unless the worker is running it.

    The status check and the write are one statement, so the worker cannot
    claim the job between them. Raises JobNotFound or JobRunning.
    """
    cur = conn.execute(
        "UPDATE jobs SET handbrake_args = ? WHERE id = ? AND status != ?",
        (args.to_json(), job_id, JobStatus.RUNNING.value),
    )
    conn.commit()
    if cur.rowcount == 1:
        return
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise JobNotFound(job_id)
    raise JobRunning(job_id)


def update_job(
    conn: sqlite3.Connection,
    job_id: int,
    args: HandbrakeArgs,
    *,
    show: str | None,
    season: int | None,
    episode: int | None,
    movie: str | None,
    output_path: str,
    year: int | None = None,
    tvdb_id: int | None = None,
) -> None:
    """Replace a job's settings and naming together, unless it is running.

    Same single-statement guard as update_job_args. Creates the new output
    directory. Raises JobNotFound or JobRunning.
    """
    cur = conn.execute(
        """
        UPDATE jobs
        SET    handbrake_args = ?, show = ?, season = ?, episode = ?, movie = ?,
               output_path = ?, year = ?, tvdb_id = ?
        WHERE  id = ? AND status != ?
        """,
        (args.to_json(), show, season, episode, movie, output_path, year, tvdb_id,
         job_id, JobStatus.RUNNING.value),
    )
    conn.commit()
    if cur.rowcount != 1:
        if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            raise JobNotFound(job_id)
        raise JobRunning(job_id)
    pathlib.Path(output_path).parent.mkdir(parents=True, exist_ok=True)


def load_source_summary(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    probe_file: Callable[[str], dict] = probe.probe,
) -> tuple[probe.StreamSummary, list[probe.Chapter] | None]:
    """The job's source track layout, from its snapshot or a fresh probe.

    Jobs queued before v0.6 have no snapshot: the source is probed once and
    the result saved. Raises JobNotFound, or whatever probe_file raises when
    the source is gone or unreadable.
    """
    row = conn.execute(
        "SELECT source_path, source_probe FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise JobNotFound(job_id)
    if row["source_probe"]:
        try:
            return probe.snapshot_from_json(row["source_probe"])
        except ValueError:
            pass  # unreadable snapshot: fall through and re-probe
    summary = probe.summarize(probe_file(row["source_path"]))
    conn.execute(
        "UPDATE jobs SET source_probe = ? WHERE id = ?",
        (probe.snapshot_to_json(summary), job_id),
    )
    conn.commit()
    return summary, None


# ---------------------------------------------------------------------------
# Editing one job or a whole batch
# ---------------------------------------------------------------------------

KEEP = object()  # plan_edits: leave each job's own value


@dataclass
class EditTarget:
    """A job as an editor sees it: identity, settings, and its source tracks."""

    job_id: int
    status: str
    is_tv: bool
    title: str  # show or movie name
    season: int | None
    episode: int | None
    output_path: str
    args: HandbrakeArgs
    summary: probe.StreamSummary | None  # None when the source can't be read
    probe_error: str | None = None
    year: int | None = None
    tvdb_id: int | None = None

    @property
    def label(self) -> str:
        if self.is_tv:
            return naming.episode_label(self.season, self.episode)  # type: ignore[arg-type]
        return naming.folder_name(self.title, self.year)


@dataclass
class EditResult:
    job_id: int
    label: str
    args: HandbrakeArgs
    show: str | None
    season: int | None
    episode: int | None
    movie: str | None
    output_path: str
    errors: list[str] = field(default_factory=list)
    year: int | None = None
    tvdb_id: int | None = None


def load_edit_targets(
    conn: sqlite3.Connection,
    job_ids: list[int],
    *,
    probe_file: Callable[[str], dict] = probe.probe,
) -> list[EditTarget]:
    """Load jobs for editing, in the order given. Raises JobNotFound."""
    targets = []
    for job_id in job_ids:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        summary, error = None, None
        try:
            summary, _ = load_source_summary(conn, job_id, probe_file=probe_file)
        except Exception as exc:  # missing/unreadable source: still editable
            source = row["source_path"]
            if not pathlib.Path(source).exists():
                error = f"source file not found: {source}"
            else:
                error = f"ffprobe could not read {source}: {exc}"
        is_tv = row["show"] is not None
        targets.append(EditTarget(
            job_id=row["id"],
            status=row["status"],
            is_tv=is_tv,
            title=row["show"] if is_tv else row["movie"],
            season=row["season"],
            episode=row["episode"],
            output_path=row["output_path"],
            args=HandbrakeArgs.from_json(row["handbrake_args"]),
            summary=summary,
            probe_error=error,
            year=row["year"],
            tvdb_id=row["tvdb_id"],
        ))
    return targets


def batch_job_ids(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    """Editable (not running) jobs of a batch, in episode order."""
    rows = conn.execute(
        """
        SELECT id FROM jobs
        WHERE  ingest_batch_id = ? AND status != ?
        ORDER BY season, episode, id
        """,
        (batch_id, JobStatus.RUNNING.value),
    ).fetchall()
    return [r["id"] for r in rows]


def _settings_for(job_args: HandbrakeArgs, template: HandbrakeArgs) -> HandbrakeArgs:
    """The template's settings, keeping what is specific to this job."""
    out = plan.with_chapters(template, job_args.chapter_start, job_args.chapter_end)
    out.encoder = job_args.encoder
    out.extra_args = list(job_args.extra_args)
    return out


def plan_edits(
    targets: list[EditTarget],
    *,
    template: HandbrakeArgs | None,
    title: str,
    season: int | None = None,
    first_episode: int | None = None,
    year: int | None | object = KEEP,
    tvdb_id: int | None | object = KEEP,
) -> list[EditResult]:
    """Work out each job's new settings and naming. Pure.

    template None keeps every job's settings. For TV, episodes are renumbered
    from first_episode in the targets' current episode order; first_episode
    None keeps them. year/tvdb_id KEEP leaves each job's own. Each result lists
    its validation errors.
    """
    ordered = sorted(targets, key=lambda t: (t.season or 0, t.episode or 0, t.job_id))
    position = {t.job_id: i for i, t in enumerate(ordered)}
    results = []
    for t in targets:
        args = t.args if template is None else _settings_for(t.args, template)
        new_year = t.year if year is KEEP else year
        new_tvdb = t.tvdb_id if tvdb_id is KEEP else tvdb_id
        errors: list[str] = []
        if t.summary is not None:
            errors += plan.validate(args, t.summary)
        if t.is_tv:
            new_season = t.season if season is None else season
            new_episode = (
                t.episode if first_episode is None else first_episode + position[t.job_id]
            )
            output = naming.renamed_output(
                t.output_path, is_tv=True, old_title=t.title, new_title=title,
                season=new_season, episode=new_episode, old_year=t.year, new_year=new_year,  # type: ignore[arg-type]
            )
            label = naming.episode_label(new_season, new_episode)  # type: ignore[arg-type]
            show, movie = title, None
        else:
            new_season = new_episode = None
            output = naming.renamed_output(
                t.output_path, is_tv=False, old_title=t.title, new_title=title,
                old_year=t.year, new_year=new_year,  # type: ignore[arg-type]
            )
            label = naming.folder_name(title, new_year)  # type: ignore[arg-type]
            show, movie = None, title
        results.append(EditResult(
            job_id=t.job_id, label=label, args=args, show=show, season=new_season,
            episode=new_episode, movie=movie, output_path=str(output),
            errors=[f"{label}: {e}" for e in errors],
            year=new_year, tvdb_id=new_tvdb,  # type: ignore[arg-type]
        ))
    outputs = [r.output_path for r in results]
    for r in results:
        if outputs.count(r.output_path) > 1:
            r.errors.append(f"{r.label}: output {r.output_path} is used by more than one job")
    return results


def apply_edits(conn: sqlite3.Connection, results: list[EditResult]) -> tuple[list[str], bool]:
    """Save planned edits. Returns one line per job, and whether all were saved."""
    lines, ok = [], True
    for r in results:
        try:
            update_job(
                conn, r.job_id, r.args, show=r.show, season=r.season,
                episode=r.episode, movie=r.movie, output_path=r.output_path,
                year=r.year, tvdb_id=r.tvdb_id,
            )
            lines.append(f"Job {r.job_id} ({r.label}) updated.")
        except JobRunning:
            lines.append(f"Job {r.job_id} ({r.label}) is now running; not changed.")
            ok = False
        except JobNotFound:
            lines.append(f"Job {r.job_id} no longer exists; not changed.")
            ok = False
    return lines, ok
