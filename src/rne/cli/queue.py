from __future__ import annotations

import pathlib
import sys

from rne import db
from rne.cli._pipeline import (
    create_batch_row,
    insert_jobs,
    mungefilename,
    preview_and_confirm,
    probe_and_display,
    prompt_encoding_config,
    prompt_metadata,
)
from rne.models import HandbrakeArgs


# ---------------------------------------------------------------------------
# Job plan construction (queue-specific: source files stay in place)
# ---------------------------------------------------------------------------


def _build_jobs_plan_queue(
    *,
    is_tv: bool,
    show: str | None,
    season: int | None,
    episodes: list[int] | None,
    movie: str | None,
    staging_dir: pathlib.Path,
    source_paths: list[pathlib.Path],
    hb_args: HandbrakeArgs,
) -> list[dict]:
    """Build job plan dicts from user-supplied source paths.

    source_path on each job points at the original file — no copying or moving.
    """
    jobs = []
    for pos, file_path in enumerate(source_paths):
        source = str(file_path)

        if is_tv:
            ep = episodes[pos]  # type: ignore[index]
            out_dir = staging_dir / f"Season {season:02d}"
            out_name = f"{show} - S{season:02d}E{ep:02d}.mkv"
            out_path = str(out_dir / out_name)
            label = f"S{season:02d}E{ep:02d}"
            episode_out: int | None = ep
        else:
            if len(source_paths) == 1:
                out_name = f"{movie}.mkv"
            else:
                out_name = f"{movie}_{pos:02d}.mkv"
            out_path = str(staging_dir / out_name)
            label = movie  # type: ignore[assignment]
            episode_out = None

        jobs.append(
            {
                "label": label,
                "show": show,
                "season": season,
                "episode": episode_out,
                "movie": movie,
                "source_path": source,
                "output_path": out_path,
                "handbrake_args": hb_args,
                "layout_warning": False,
            }
        )
    return jobs


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_manifest(path: pathlib.Path) -> list[pathlib.Path]:
    """Resolve path to an ordered list of .mkv files.

    - File: returns [path].
    - Directory: returns sorted glob of *.mkv, printing the file list.
    - Missing: prints error and calls sys.exit(1).
    - Empty directory: prints friendly message and calls sys.exit(0).
    """
    if not path.exists():
        print(f"Error: {path} does not exist.", file=sys.stderr)
        sys.exit(1)

    if path.is_file():
        if path.suffix.lower() != ".mkv":
            print(f"Error: {path} is not a .mkv file.", file=sys.stderr)
            sys.exit(1)
        print("Queueing 1 file.")
        return [path]

    # Directory
    files = sorted(path.glob("*.mkv"))
    if not files:
        print(f"No .mkv files in {path}, nothing to queue.")
        sys.exit(0)

    print(f"Found {len(files)} file(s) in {path}:")
    for i, f in enumerate(files, 1):
        print(f"  {i}. {f.name}")
    print(
        "Files will be processed in this order. "
        "If the order is wrong, rename the files or queue them one at a time."
    )
    return files


# ---------------------------------------------------------------------------
# Main queue flow
# ---------------------------------------------------------------------------


def run(args) -> None:
    path = pathlib.Path(args.path).expanduser().resolve()

    # ---- Step 1: resolve path --------------------------------------------------
    manifest = _resolve_manifest(path)

    # ---- Step 2: content classification and naming -----------------------------
    # The hint default is the directory basename (dir mode) or file stem (file mode).
    name_hint = path.name if path.is_dir() else path.stem
    # name_hint may be garbage but is offered as a convenience default, same as
    # the disc volume name default in ingest.
    name_hint = mungefilename(name_hint)

    print()
    is_tv, show, season, first_ep, movie = prompt_metadata(name_hint, len(manifest))

    episodes: list[int] | None = None
    if is_tv:
        episodes = list(range(first_ep, first_ep + len(manifest)))  # type: ignore[arg-type]

    # ---- Step 3: create ingest_batches row (no raw dir) -----------------------
    # notes records the original source location for traceability.
    conn = db.connect()
    db.init_db(conn)
    notes = f"Queued from {path.absolute()}"
    batch_id = create_batch_row(
        conn,
        is_tv=is_tv,
        show=show,
        movie=movie,
        season=season,
        notes=notes,
        label_suffix=" (queued)",
    )
    # No raw dir is created — source files stay exactly where the user put them.

    # ---- Step 4: probe first file ---------------------------------------------
    first_source = manifest[0]
    stream_summary = probe_and_display(first_source)

    # ---- Step 5: encoding config -----------------------------------------------
    print()
    hb_args = prompt_encoding_config(stream_summary, is_dvd=args.dvd)

    # ---- Step 6: build job plan ------------------------------------------------
    # Output goes to staging under the show/movie name, same as ingest.
    from rne import config
    staging_dir = pathlib.Path(config.STAGING_ROOT) / (
        show if is_tv else movie  # type: ignore[arg-type]
    )

    jobs_plan = _build_jobs_plan_queue(
        is_tv=is_tv,
        show=show,
        season=season,
        episodes=episodes,
        movie=movie,
        staging_dir=staging_dir,
        source_paths=manifest,
        hb_args=hb_args,
    )

    # ---- Step 7: preview, mismatch detection, confirm --------------------------
    remaining_paths = manifest[1:]
    jobs_plan = preview_and_confirm(jobs_plan, stream_summary, remaining_paths)

    # ---- Step 8: insert and exit -----------------------------------------------
    insert_jobs(conn, batch_id, jobs_plan)

    print(
        f"\nQueued {len(jobs_plan)} job(s) (batch {batch_id}) from {path.absolute()}."
    )
    print(
        "Source files left in place — do not move or delete them "
        "until encoding completes."
    )
