from __future__ import annotations

import pathlib
import subprocess
import sys

from rne import config, db, makemkv, notify, plan, probe
from rne.cli._pipeline import (
    mungefilename,
    preview_and_confirm,
    probe_and_display,
    prompt_disc_split,
    prompt_encoding_config,
    prompt_metadata,
)
from rne.cli import prompts
from rne.jobs import create_batch_row, insert_jobs
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
        if is_tv:
            jobs.append(plan.tv_job(
                show=show,  # type: ignore[arg-type]
                season=season,  # type: ignore[arg-type]
                episode=episodes[idx_pos],  # type: ignore[index]
                show_dir=staging_dir,
                source_path=file_path,
                args=hb_args,
            ))
        else:
            # Several titles under one movie name are told apart by disc title.
            suffix = "" if len(rip_manifest) == 1 else f"_t{title_idx:02d}"
            jobs.append(plan.movie_job(
                movie=movie,  # type: ignore[arg-type]
                movie_dir=staging_dir,
                source_path=file_path,
                args=hb_args,
                suffix=suffix,
            ))
    return jobs


# ---------------------------------------------------------------------------
# Rip with retries
# ---------------------------------------------------------------------------


def _rip_title_with_retries(
    title_idx: int,
    *,
    source: str,
    raw_dir: pathlib.Path,
    minlength: int,
    auto_retries: int,
) -> pathlib.Path | None:
    """Rip one title, automatically retrying up to auto_retries times.

    Once automatic retries are exhausted, asks the user whether to abort the
    whole ingest, retry once more, or skip the title. Returns the ripped file
    path, or None if the user chose to skip. Calls sys.exit(1) on abort.
    """
    attempt = 0
    while True:
        try:
            return makemkv.rip_and_detect(
                source=source, title_idx=title_idx, raw_dir=raw_dir, minlength=minlength
            )
        except (subprocess.CalledProcessError, makemkv.MakemkvError):
            if attempt < auto_retries:
                attempt += 1
                print(
                    f"\nTitle {title_idx} failed; "
                    f"retrying ({attempt} of {auto_retries}) ...",
                    file=sys.stderr,
                )
                continue
            print(
                f"\nTitle {title_idx} failed. "
                "Abort the whole ingest, retry the title, or skip and continue? [a/r/s]"
            )
            while True:
                c = input("> ").strip().lower()
                if c == "a":
                    print("Aborting.", file=sys.stderr)
                    sys.exit(1)
                if c == "r":
                    break  # back to the rip loop for another attempt
                if c == "s":
                    return None
                print(
                    "Please enter 'a' to abort, 'r' to retry, or 's' to skip.",
                    file=sys.stderr,
                )


# ---------------------------------------------------------------------------
# Rip-complete notification
# ---------------------------------------------------------------------------


def _load_notify_config_or_exit() -> config.NotifyConfig:
    """Read [notifications] up front, so a typo fails before the disc spins up."""
    try:
        return config.load_notify_config()
    except config.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def library_or_exit() -> config.LibraryConfig:
    """The [library] roots; exits 2 on a malformed table."""
    try:
        return config.load_library_config()
    except config.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def tvdb_lookup_or_exit():
    """TVDB search function (or None) plus why it's off; exits 2 on a bad [tvdb]."""
    from rne import tvdb

    try:
        return tvdb.lookup_from_config()
    except config.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def notify_message(cfg: config.NotifyConfig, **values: object) -> str | None:
    """Best-effort 'the disc is done' push. Never raises, never blocks for long.

    Returns the one line to show (None when notifications aren't set up).
    """
    if cfg.mqtt is None:
        return f"Notification skipped: {cfg.skipped}." if cfg.skipped else None
    result = notify.send(cfg.mqtt, values)
    return f"Notification {'sent' if result.ok else 'failed'}: {result.detail}"


def _notify_rip_complete(cfg: config.NotifyConfig, **values: object) -> None:
    """Print the notification result. The user is standing by to answer the
    track prompts, and a broker that is down is not their problem right now.
    """
    message = notify_message(cfg, **values)
    if message:
        print(f"\n{message}", file=sys.stderr if "failed:" in message else sys.stdout)


# ---------------------------------------------------------------------------
# Main ingest flow
# ---------------------------------------------------------------------------


def run(args, roots: config.Roots) -> None:
    if getattr(args, "plain", False):
        run_plain(args, roots)
    else:
        run_tui(args, roots)


def run_tui(args, roots: config.Roots) -> None:
    """Full-screen ingest: titles → naming → rip → tracks → review."""
    from rne.tui import flow
    from rne.tui.pipeline import PipelineApp

    notify_cfg = _load_notify_config_or_exit()
    lookup, lookup_note = tvdb_lookup_or_exit()
    library = library_or_exit()
    print("Reading disc ...")
    try:
        disc_info, titles = makemkv.run_info(source=args.source, minlength=args.minlength)
    except subprocess.CalledProcessError:
        sys.exit(1)
    if not titles:
        print("No titles found on disc.", file=sys.stderr)
        sys.exit(1)
    volume_name = disc_info.get(makemkv.C_VOLUME_NAME, "UNKNOWN")

    conn = db.connect()
    db.init_db(conn)
    draft = flow.Draft(
        mode="ingest",
        naming=flow.Naming(
            is_tv=True,
            title=mungefilename(args.name) if args.name is not None else volume_name,
            season=args.season,
            first_episode=args.first_episode,
        ),
        staging_root=roots.staging_root,
        library=library,
    )
    app = PipelineApp(
        draft,
        conn=conn,
        prober=lambda path: probe.summarize(probe.probe(str(path))),
        chapter_prober=lambda path: probe.probe_chapters(str(path)),
        titles=titles,
        display_order=_build_display_order(titles),
        volume_name=volume_name,
        ripper=lambda idx, raw_dir, on_event: makemkv.rip_title(
            args.source, idx, raw_dir, args.minlength, on_event
        ),
        notifier=lambda **values: notify_message(notify_cfg, **values),
        auto_retries=config.RIP_RETRIES,
        batch_notes=volume_name,
        lookup=lookup,
        lookup_note=lookup_note,
    )
    finish(app.run())


def finish(result: tuple[list[str], bool] | None) -> None:
    """Print what the app reported and exit non-zero if it failed."""
    from rne.tui.common import clear_after_app

    clear_after_app()
    lines, ok = result or (["Quit; nothing was queued."], True)
    for line in lines:
        print(line, file=sys.stdout if ok else sys.stderr)
    if not ok:
        sys.exit(1)


def run_plain(args, roots: config.Roots) -> None:
    """The pre-v0.6 line-by-line prompts (rne ingest --plain)."""
    minlength: int = args.minlength
    source: str = args.source
    notify_cfg = _load_notify_config_or_exit()

    # ---- Step 1: disc detection ------------------------------------------------
    try:
        disc_info, titles = makemkv.run_info(source=source, minlength=minlength)
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
        name=args.name,
        season=args.season,
        first_ep=args.first_episode,
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
    staging_dir = roots.staging_root / (show if is_tv else movie)  # type: ignore[arg-type]

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
        file_path = _rip_title_with_retries(
            title_idx,
            source=source,
            raw_dir=raw_dir,
            minlength=minlength,
            auto_retries=config.RIP_RETRIES,
        )
        if file_path is not None:
            rip_manifest.append((title_idx, file_path))

    if not rip_manifest:
        print("No titles survived. Aborting.", file=sys.stderr)
        sys.exit(1)

    # The drive has spun down and everything from here on is interactive, so
    # this is the moment worth being told about from the other room.
    _notify_rip_complete(
        notify_cfg,
        disc=volume_name,
        title=show if is_tv else movie,
        kind="tv" if is_tv else "movie",
        season=season,
        count=len(rip_manifest),
        batch=batch_id,
    )

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
