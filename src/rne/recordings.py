"""Queue Jellyfin Live TV recordings for encoding.

Jellyfin's DVR post-processing hook can only pass the recording's path, so
everything else comes from the files Jellyfin writes around it:

    <recordings_root>/Series/<Show>/<recording>.ts
    <recordings_root>/Series/<Show>/<recording>.nfo   <episodedetails>: season, episode
    <recordings_root>/Series/<Show>/tvshow.nfo        <tvshow>: title (maybe year)
    <recordings_root>/Movies/.../<recording>.ts
    <recordings_root>/Movies/.../<recording>.nfo      <movie>: title, year

A series recording without a season and episode number (sports, news) is
skipped: nothing is queued. The recording itself is never moved or deleted.

The broadcast date says nothing about the show's year, so the library
folder is found rather than guessed: an existing "Show (2025)" or "Show"
folder is reused, otherwise a plain "Show" folder is used.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

from rne import config, jobs, naming, plan, probe
from rne.cli._pipeline import mungefilename

SERIES_DIR = "Series"
MOVIES_DIR = "Movies"
BATCH_SUFFIX = " (Live TV)"

_YEAR_SUFFIX = re.compile(r"^(?P<title>.+?) \((?P<year>\d{4})\)$")


class RecordingError(ValueError):
    """The request can't be acted on: bad path, unreadable file or .nfo."""


@dataclass(frozen=True)
class Recording:
    is_tv: bool
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True)
class Outcome:
    status: str  # "queued", "exists" or "skipped"
    job_id: int | None = None
    label: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# .nfo files (pure)
# ---------------------------------------------------------------------------


def _parse_xml(text: str, what: str) -> ET.Element:
    try:
        return ET.fromstring(text.lstrip("﻿").strip())
    except ET.ParseError as exc:
        raise RecordingError(f"{what} is not valid XML: {exc}") from None


def _text(root: ET.Element, tag: str) -> str:
    el = root.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _int(root: ET.Element, tag: str) -> int | None:
    text = _text(root, tag)
    return int(text) if text.isdigit() else None


def _year(root: ET.Element) -> int | None:
    year = _int(root, "year")
    if year is None:
        premiered = _text(root, "premiered")
        year = int(premiered[:4]) if premiered[:4].isdigit() else None
    return year if year and 1880 <= year <= 2100 else None


def parse_episode_nfo(text: str) -> tuple[int | None, int | None]:
    """(season, episode) from an <episodedetails> .nfo; None for a missing number."""
    root = _parse_xml(text, "episode .nfo")
    return _int(root, "season"), _int(root, "episode")


def parse_show_nfo(text: str) -> tuple[str, int | None]:
    """(title, year) from a tvshow.nfo."""
    root = _parse_xml(text, "tvshow.nfo")
    return _text(root, "title"), _year(root)


def parse_movie_nfo(text: str) -> tuple[str, int | None]:
    """(title, year) from a <movie> .nfo."""
    root = _parse_xml(text, "movie .nfo")
    return _text(root, "title"), _year(root)


def split_year(name: str) -> tuple[str, int | None]:
    """'Show (2025)' -> ('Show', 2025); anything else -> (name, None)."""
    m = _YEAR_SUFFIX.match(name)
    return (m["title"], int(m["year"])) if m else (name, None)


# ---------------------------------------------------------------------------
# Reading a recording from disk
# ---------------------------------------------------------------------------


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecordingError(f"can't read {path}: {exc.strerror or exc}") from None


def describe(path: pathlib.Path, recordings_root: pathlib.Path) -> Recording | str:
    """What a recording is, or why it should be skipped (a str).

    Raises RecordingError for a path rne cannot accept at all.
    """
    if not path.is_absolute():
        raise RecordingError(f"path must be absolute: {path}")
    try:
        rel = path.resolve().relative_to(recordings_root.resolve())
    except ValueError:
        raise RecordingError(f"{path} is not under {recordings_root}") from None
    if not path.is_file():
        raise RecordingError(f"recording not found: {path}")
    parts = rel.parts
    kind = parts[0].casefold() if len(parts) > 1 else ""

    if kind == SERIES_DIR.casefold():
        if len(parts) < 3:
            return "series recordings are expected in Series/<Show>/"
        show_dir = recordings_root / parts[0] / parts[1]
        episode_nfo = _read(path.with_suffix(".nfo"))
        if episode_nfo is None:
            return f"no episode .nfo next to the recording ({path.with_suffix('.nfo').name})"
        season, episode = parse_episode_nfo(episode_nfo)
        if season is None or episode is None:
            return "the recording has no season and episode number"
        show_nfo = _read(show_dir / "tvshow.nfo")
        title, year = parse_show_nfo(show_nfo) if show_nfo is not None else ("", None)
        if not title:
            title, year = split_year(show_dir.name)
        return Recording(True, mungefilename(title).strip(), year, season, episode)

    if kind == MOVIES_DIR.casefold():
        nfo = _read(path.with_suffix(".nfo")) or _read(path.parent / "movie.nfo")
        title, year = parse_movie_nfo(nfo) if nfo is not None else ("", None)
        if not title:
            name = path.parent.name if len(parts) > 2 else path.stem
            title, year = split_year(name)
        return Recording(False, mungefilename(title).strip(), year)

    return f"recordings are expected under {SERIES_DIR}/ or {MOVIES_DIR}/"


def find_library_folder(
    root: pathlib.Path, title: str, year: int | None
) -> tuple[pathlib.Path, int | None]:
    """The show/movie folder to use in a library root, and the year it carries.

    Reuses an existing folder: "Title (year)" when the year is known, else
    "Title", else the only "Title (YYYY)". Otherwise the folder is new:
    "Title (year)" with a known year, plain "Title" without.
    """
    want = title.casefold()
    plain, dated = None, []
    try:
        entries = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        entries = []
    for entry in sorted(entries):
        name, folder_year = split_year(entry.name)
        if name.casefold() != want:
            continue
        if folder_year is None:
            plain = entry
        else:
            dated.append((entry, folder_year))
    if year is not None:
        for entry, folder_year in dated:
            if folder_year == year:
                return entry, year
    if plain is not None:
        return plain, None
    if len(dated) == 1 and year is None:
        return dated[0]
    return root / naming.folder_name(title, year), year


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


def enqueue(
    conn: sqlite3.Connection,
    path: str | pathlib.Path,
    *,
    recordings_root: pathlib.Path,
    staging_root: pathlib.Path,
    library: config.LibraryConfig,
    probe_file: Callable[[str], dict] | None = None,
) -> Outcome:
    """Queue one recording at the end of the queue. Raises RecordingError."""
    path = pathlib.Path(path)
    existing = conn.execute(
        "SELECT id, status FROM jobs WHERE source_path = ? ORDER BY id DESC LIMIT 1",
        (str(path),),
    ).fetchone()
    if existing is not None:
        return Outcome("exists", existing["id"],
                       reason=f"already job {existing['id']} ({existing['status']})")

    rec = describe(path, recordings_root)
    if isinstance(rec, str):
        return Outcome("skipped", reason=rec)
    if not rec.title:
        raise RecordingError(f"no title found for {path}")

    root = library.root_for(rec.is_tv)
    lib_dir, year = (find_library_folder(root, rec.title, rec.year) if root
                     else (None, rec.year))
    try:
        summary = probe.summarize((probe_file or probe.probe)(str(path)))
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        raise RecordingError(f"ffprobe could not read {path}: {exc}") from None
    args = plan.livetv_args(summary)
    folder = staging_root / naming.folder_name(rec.title, year)
    if rec.is_tv:
        job = plan.tv_job(show=rec.title, season=rec.season, episode=rec.episode,  # type: ignore[arg-type]
                          show_dir=folder, source_path=path, args=args, year=year)
    else:
        job = plan.movie_job(movie=rec.title, movie_dir=folder, source_path=path,
                             args=args, year=year)
    clash = conn.execute(
        "SELECT id FROM jobs WHERE output_path = ? AND status NOT IN ('cancelled', 'failed')",
        (job["output_path"],),
    ).fetchone()
    if clash is not None:
        return Outcome("skipped", clash["id"], job["label"],
                       f"{job['label']} of {rec.title} is already job {clash['id']}; "
                       "re-encode or edit that one instead")
    job["source_summary"] = summary
    if lib_dir is not None:
        job["final_path"] = str(naming.library_path(lib_dir, job["output_path"],
                                                    is_tv=rec.is_tv))
    batch_id = jobs.create_batch_row(
        conn, is_tv=rec.is_tv, show=rec.title if rec.is_tv else None,
        movie=None if rec.is_tv else rec.title, season=rec.season,
        notes=f"Live TV recording: {path}", label_suffix=BATCH_SUFFIX, year=year,
    )
    jobs.insert_jobs(conn, batch_id, [job])
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE ingest_batch_id = ?", (batch_id,)
    ).fetchone()["id"]
    return Outcome("queued", job_id, job["label"])
