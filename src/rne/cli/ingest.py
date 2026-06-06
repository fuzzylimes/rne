from __future__ import annotations

import pathlib
import subprocess
import sys

from rne import config, db, makemkv
from rne.cli._pipeline import (
    create_batch_row,
    insert_jobs,
    preview_and_confirm,
    probe_and_display,
    prompt_disc_split,
    prompt_encoding_config,
    prompt_metadata,
)
from rne.cli import prompts
from rne.models import HandbrakeArgs


# ---------------------------------------------------------------------------
# Ingest-specific table display (uses makemkv.summarize)
# ---------------------------------------------------------------------------


def _build_display_order(titles: dict) -> list[int]:
    """Return disc title indexes sorted by .mpls source filename."""
    return sorted(
        titles.keys(),
        key=lambda idx: titles[idx]["info"].get(makemkv.T_SOURCE, ""),
    )


def _print_title_table(titles: dict) -> list[int]:
    """Print title table sorted by .mpls source name. Returns disc indexes in display order."""
    from rne.cli._pipeline import _print_table
    display_order = _build_display_order(titles)
    cols = ["#", "Disc Index", "Source", "Duration", "Size",
            "Ch", "Resolution", "FPS", "Audio"]
    rows = []
    for display_idx, disc_idx in enumerate(display_order):
        row = makemkv.summarize(disc_idx, titles[disc_idx])
        row["#"] = display_idx
        row["Disc Index"] = disc_idx
        rows.append(row)
    _print_table(cols, rows)
    return display_order


# ---------------------------------------------------------------------------
# Job plan construction (ingest-specific: uses rip_manifest with title_idx)
# ---------------------------------------------------------------------------


def _build_jobs_plan(
    *,
    is_tv: bool,
    show: str | None,
    season: int | None,
    episodes: list[int] | None,
    movie: str | None,
    staging_dir: pathlib.Path,
    rip_manifest: list[tuple[int, pathlib.Path]],
    hb_args: HandbrakeArgs,
) -> list[dict]:
    """Build a list of job plan dicts from the rip manifest.

    rip_manifest is an ordered list of (title_idx, file_path) as produced
    by the rip loop — the source_path for each job is taken directly from
    the manifest rather than being constructed from a title_tNN.mkv template.
    """
    jobs = []
    for idx_pos, (title_idx, file_path) in enumerate(rip_manifest):
        source = str(file_path)

        if is_tv:
            ep = episodes[idx_pos]  # type: ignore[index]
            out_dir = staging_dir / f"Season {season:02d}"
            out_name = f"{show} - S{season:02d}E{ep:02d}.mkv"
            out_path = str(out_dir / out_name)
            label = f"S{season:02d}E{ep:02d}"
            episode_out: int | None = ep
        else:
            if len(rip_manifest) == 1:
                out_name = f"{movie}.mkv"
            else:
                out_name = f"{movie}_t{title_idx:02d}.mkv"
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
# Main ingest flow
# ---------------------------------------------------------------------------


def run(args) -> None:
    minlength: int = args.minlength

    # ---- Step 1: disc detection ------------------------------------------------
    try:
        disc_info, titles = makemkv.run_info(disc=0, minlength=minlength)
    except subprocess.CalledProcessError:
        sys.exit(1)

    if not titles:
        print("No titles found on disc.", file=sys.stderr)
        sys.exit(1)

    volume_name = disc_info.get(makemkv.C_VOLUME_NAME, "UNKNOWN")
    print(f"\nDisc: {volume_name}\n")
    display_order = _print_title_table(titles)

    # ---- Step 2: title selection -----------------------------------------------
    # User selects by display # (mpls order). We map back to disc indexes so
    # ripping happens in mpls order, giving correct sequential episode assignment.
    valid_display = list(range(len(display_order)))
    print()
    raw_sel = input(
        "Titles to rip (e.g. '0-7', '0,2,4', 'all', empty to abort): "
    ).strip()
    if not raw_sel:
        print("Aborted.")
        sys.exit(0)

    parsed_sel = makemkv.parse_index_spec(raw_sel)
    if parsed_sel is None:
        selected_display = valid_display
    else:
        invalid = [i for i in parsed_sel if i not in valid_display]
        if invalid:
            print(f"Invalid title indexes: {invalid}", file=sys.stderr)
            sys.exit(1)
        selected_display = parsed_sel

    if not selected_display:
        print("No titles selected. Aborted.", file=sys.stderr)
        sys.exit(1)

    selected_indexes = [display_order[i] for i in selected_display]

    # ---- Step 3: content classification and naming -----------------------------
    print()
    is_tv, show, season, first_ep, movie, is_disc_split = prompt_metadata(
        volume_name,
        len(selected_indexes),
        single_file_tv=(len(selected_indexes) == 1),
    )

    episodes: list[int] | None = None
    if is_tv and not is_disc_split:
        episodes = list(range(first_ep, first_ep + len(selected_indexes)))  # type: ignore[arg-type]

    # ---- Step 4: staging dir confirm, create batch row, rip per title ----------
    #
    # Order of operations (sanity-checked against spec):
    #   a. INSERT into ingest_batches → capture batch_id
    #   b. Construct raw_dir using batch_id
    #   c. mkdir(exist_ok=False) — fresh id must produce a fresh dir
    #   d. Rip loop: rip_and_detect per title, build rip_manifest in disc order
    staging_dir = pathlib.Path(config.STAGING_ROOT) / \
        (show if is_tv else movie)  # type: ignore[arg-type]

    conn = db.connect()
    db.init_db(conn)
    batch_id = create_batch_row(
        conn,
        is_tv=is_tv,
        show=show,
        movie=movie,
        season=season,
        notes=volume_name,
    )

    raw_dir = staging_dir / "_raw" / f"batch-{batch_id}"

    print()
    if not prompts.prompt_yes_no(f"Rip to {raw_dir}/"):
        staging_dir = pathlib.Path(input("Staging directory: ").strip())
        raw_dir = staging_dir / "_raw" / f"batch-{batch_id}"

    raw_dir.mkdir(parents=True, exist_ok=False)

    rip_manifest: list[tuple[int, pathlib.Path]] = []

    for title_idx in selected_indexes:
        try:
            file_path = makemkv.rip_and_detect(
                disc=0, title_idx=title_idx, raw_dir=raw_dir, minlength=minlength
            )
            rip_manifest.append((title_idx, file_path))
        except (subprocess.CalledProcessError, makemkv.MakemkvError):
            print(
                f"\nTitle {title_idx} failed. "
                "Abort the whole ingest, or skip and continue? [a/s]"
            )
            while True:
                c = input("> ").strip().lower()
                if c == "a":
                    print("Aborting.", file=sys.stderr)
                    sys.exit(1)
                if c == "s":
                    break
                print("Please enter 'a' to abort or 's' to skip.", file=sys.stderr)

    if not rip_manifest:
        print("No titles survived. Aborting.", file=sys.stderr)
        sys.exit(1)

    surviving_episodes: list[int] | None = None
    if is_tv and not is_disc_split:
        episode_by_idx = dict(zip(selected_indexes, episodes))  # type: ignore[arg-type]
        surviving_episodes = [episode_by_idx[ti] for ti, _ in rip_manifest]

    # ---- Step 5: probe first ripped file ---------------------------------------
    first_source = rip_manifest[0][1]
    stream_summary = probe_and_display(first_source)

    # ---- Step 6: encoding config -----------------------------------------------
    print()
    hb_args = prompt_encoding_config(stream_summary)

    # ---- Step 7: preview, mismatch detection, edit, confirm --------------------
    if is_disc_split:
        from rne import probe as probe_mod
        try:
            chapters = probe_mod.probe_chapters(str(first_source))
        except Exception as exc:
            print(f"Chapter probe failed: {exc}", file=sys.stderr)
            sys.exit(1)
        jobs_plan = prompt_disc_split(
            source_path=first_source,
            chapters=chapters,
            start_ep=first_ep,  # type: ignore[arg-type]
            show=show,  # type: ignore[arg-type]
            season=season,  # type: ignore[arg-type]
            staging_dir=staging_dir,
            hb_args=hb_args,
        )
        jobs_plan = preview_and_confirm(jobs_plan, stream_summary, [])
    else:
        jobs_plan = _build_jobs_plan(
            is_tv=is_tv,
            show=show,
            season=season,
            episodes=surviving_episodes,
            movie=movie,
            staging_dir=staging_dir,
            rip_manifest=rip_manifest,
            hb_args=hb_args,
        )
        remaining_paths = [fp for _, fp in rip_manifest[1:]]
        jobs_plan = preview_and_confirm(jobs_plan, stream_summary, remaining_paths)

    # ---- Step 8: insert and exit -----------------------------------------------
    insert_jobs(conn, batch_id, jobs_plan)

    print(
        f"\nQueued {len(jobs_plan)} job(s) (batch {batch_id}). "
        "Worker will pick them up. Run `rne ls` to check status."
    )
