from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

from rne import config, db, makemkv, probe
from rne.cli import prompts
from rne.models import AudioTrack, HandbrakeArgs

# Characters stripped from user-supplied path components, mirroring .abcde.conf:
#   sed 's/[:><|*/\"'\''?[:cntrl:]]//g'
_MUNGE_RE = re.compile(r"""[:<>|*/\\"'?]|[\x00-\x1f\x7f]""")


def mungefilename(name: str) -> str:
    """Strip characters that are unsafe in filesystem paths."""
    return _MUNGE_RE.sub("", name)


# ---------------------------------------------------------------------------
# Table display
# ---------------------------------------------------------------------------


def _print_table(cols: list[str], rows: list[dict]) -> None:
    widths = {
        c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0))
        for c in cols
    }
    header = "  ".join(f"{c:<{widths[c]}}" for c in cols)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(f"{str(row.get(c, '')):<{widths[c]}}" for c in cols))


def _print_title_table(titles: dict) -> None:
    cols = ["#", "Source", "Duration", "Size", "Ch", "Resolution", "FPS", "Audio"]
    rows = [makemkv.summarize(tid, titles[tid]) for tid in sorted(titles)]
    _print_table(cols, rows)


def _print_stream_tables(summary: probe.StreamSummary) -> None:
    if summary.video:
        print("\nVideo:")
        vcols = ["#", "Codec", "Resolution", "FPS", "Field", "Lang", "Def", "Forced"]
        vrows = [
            {
                "#": i,
                "Codec": v.codec,
                "Resolution": v.resolution,
                "FPS": v.fps,
                "Field": v.field_order,
                "Lang": v.lang,
                "Def": "Y" if v.default else "",
                "Forced": "Y" if v.forced else "",
            }
            for i, v in enumerate(summary.video, 1)
        ]
        _print_table(vcols, vrows)

    if summary.audio:
        print("\nAudio:")
        acols = ["#", "Codec", "Ch", "Bitrate", "Lang", "Title", "Def", "Forced"]
        arows = [
            {
                "#": i,
                "Codec": a.codec,
                "Ch": str(a.channels) if a.channels is not None else "",
                "Bitrate": f"{a.bitrate // 1000}k" if a.bitrate else "",
                "Lang": a.lang,
                "Title": a.title,
                "Def": "Y" if a.default else "",
                "Forced": "Y" if a.forced else "",
            }
            for i, a in enumerate(summary.audio, 1)
        ]
        _print_table(acols, arows)

    if summary.subtitle:
        print("\nSubtitles:")
        scols = ["#", "Codec", "Lang", "Title", "Def", "Forced", "Duration"]
        srows = [
            {
                "#": i,
                "Codec": s.codec,
                "Lang": s.lang,
                "Title": s.title,
                "Def": "Y" if s.default else "",
                "Forced": "Y" if s.forced else "",
                "Duration": f"{s.duration:.0f}s" if s.duration is not None else "",
            }
            for i, s in enumerate(summary.subtitle, 1)
        ]
        _print_table(scols, srows)


# ---------------------------------------------------------------------------
# Preview (pure — tested directly)
# ---------------------------------------------------------------------------


def _audio_summary(audio_tracks: list[AudioTrack]) -> str:
    parts = []
    for t in audio_tracks:
        if t.codec == "copy":
            parts.append(f"{t.track}:copy")
        else:
            parts.append(f"{t.track}:{t.codec}@{t.bitrate}")
    return "[" + ",".join(parts) + "]"


def build_preview(jobs_plan: list[dict]) -> str:
    """Return preview text for a list of job plan dicts.

    Each dict must have: label, output_path, handbrake_args (HandbrakeArgs).
    Pure function — no I/O.
    """
    lines = ["Preview:"]
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
        out_name = pathlib.Path(job["output_path"]).name
        audio_str = _audio_summary(hb.audio_tracks)
        details = (
            f"(a={audio_str} s={hb.subtitle_tracks}"
            f" crf={hb.quality} preset={hb.preset})"
        )
        lines.append(f"  {job['label']:<8}  {out_name}  {details}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Job plan construction
# ---------------------------------------------------------------------------


def _build_jobs_plan(
    *,
    is_tv: bool,
    show: str | None,
    season: int | None,
    episodes: list[int] | None,
    movie: str | None,
    staging_dir: pathlib.Path,
    raw_dir: pathlib.Path,
    surviving_indexes: list[int],
    hb_args: HandbrakeArgs,
) -> list[dict]:
    jobs = []
    for idx_pos, title_idx in enumerate(surviving_indexes):
        source = str(raw_dir / f"title_t{title_idx:02d}.mkv")

        if is_tv:
            ep = episodes[idx_pos]  # type: ignore[index]
            out_dir = staging_dir / f"Season {season:02d}"
            out_name = f"{show} - S{season:02d}E{ep:02d}.mkv"
            out_path = str(out_dir / out_name)
            label = f"S{season:02d}E{ep:02d}"
        else:
            if len(surviving_indexes) == 1:
                out_name = f"{movie}.mkv"
            else:
                out_name = f"{movie}_t{title_idx:02d}.mkv"
            out_path = str(staging_dir / out_name)
            label = movie  # type: ignore[assignment]

        jobs.append(
            {
                "label": label,
                "show": show,
                "season": season,
                "episode": ep if is_tv else None,
                "movie": movie,
                "source_path": source,
                "output_path": out_path,
                "handbrake_args": hb_args,
            }
        )
    return jobs


# ---------------------------------------------------------------------------
# Plan serialisation for $EDITOR round-trip
# ---------------------------------------------------------------------------


def _plan_to_json(jobs_plan: list[dict]) -> str:
    serialisable = []
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
        d = dict(job)
        d["handbrake_args"] = json.loads(hb.to_json())
        serialisable.append(d)
    return json.dumps(serialisable, indent=2)


def _plan_from_json(text: str) -> list[dict]:
    """Parse and validate an edited plan. Raises ValueError on any problem."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("plan must be a JSON array")
    jobs = []
    for i, item in enumerate(data):
        hb_raw = item.get("handbrake_args")
        if hb_raw is None:
            raise ValueError(f"job {i}: missing handbrake_args")
        hb = HandbrakeArgs.from_json(json.dumps(hb_raw))
        job = dict(item)
        job["handbrake_args"] = hb
        jobs.append(job)
    return jobs


def _edit_plan(jobs_plan: list[dict]) -> list[dict]:
    """Open jobs_plan in $EDITOR and return the validated replacement."""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="rne_plan_"
    ) as f:
        f.write(_plan_to_json(jobs_plan))
        tmp = f.name

    try:
        while True:
            subprocess.run([editor, tmp], check=False)
            try:
                with open(tmp) as f:
                    return _plan_from_json(f.read())
            except Exception as exc:
                print(f"Validation error: {exc}", file=sys.stderr)
                raw = input("Re-open editor? [Y/n]: ").strip().lower()
                if raw not in ("", "y"):
                    print("Edit cancelled; keeping original plan.", file=sys.stderr)
                    return jobs_plan
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# DB insertion
# ---------------------------------------------------------------------------


def _create_ingest_batch(
    conn,
    *,
    is_tv: bool,
    show: str | None,
    movie: str | None,
    season: int | None,
    notes: str,
) -> int:
    """Create the ingest_batches row and return its id.

    Called before ripping so the batch id can be used in the raw dir path.
    """
    label = f"{show} S{season:02d}" if is_tv else str(movie)
    cur = conn.execute(
        "INSERT INTO ingest_batches (label, show, movie, notes) VALUES (?, ?, ?, ?)",
        (label, show, movie, notes),
    )
    conn.commit()
    return cur.lastrowid


def _insert_jobs(conn, batch_id: int, jobs_plan: list[dict]) -> None:
    """Insert job rows for an already-created ingest batch."""
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
        # Ensure output directory exists before the worker runs.
        pathlib.Path(job["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        conn.execute(
            """
            INSERT INTO jobs
              (show, season, episode, movie, source_path, output_path,
               handbrake_args, ingest_batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Main ingest flow
# ---------------------------------------------------------------------------


def run() -> None:
    # ---- Step 1: disc detection ------------------------------------------------
    try:
        disc_info, titles = makemkv.run_info(disc=0, minlength=0)
    except subprocess.CalledProcessError:
        sys.exit(1)

    if not titles:
        print("No titles found on disc.", file=sys.stderr)
        sys.exit(1)

    volume_name = disc_info.get(makemkv.C_VOLUME_NAME, "UNKNOWN")
    print(f"\nDisc: {volume_name}\n")
    _print_title_table(titles)

    # ---- Step 2: title selection -----------------------------------------------
    all_indexes = sorted(titles.keys())
    print()
    raw_sel = input(
        "Titles to rip (e.g. '0-7', '0,2,4', 'all', empty to abort): "
    ).strip()
    if not raw_sel:
        print("Aborted.")
        sys.exit(0)

    parsed_sel = makemkv.parse_index_spec(raw_sel)
    if parsed_sel is None:
        selected_indexes = all_indexes
    else:
        invalid = [i for i in parsed_sel if i not in all_indexes]
        if invalid:
            print(f"Invalid title indexes: {invalid}", file=sys.stderr)
            sys.exit(1)
        selected_indexes = parsed_sel

    if not selected_indexes:
        print("No titles selected. Aborted.", file=sys.stderr)
        sys.exit(1)

    # ---- Step 3: content classification ----------------------------------------
    print()
    print("What's on this disc?")
    print("  [1] TV episodes")
    print("  [2] Movie")
    while True:
        choice = input("> ").strip()
        if choice in ("1", "2"):
            break
        print("Please enter 1 or 2.", file=sys.stderr)

    is_tv = choice == "1"

    if is_tv:
        show = mungefilename(prompts.prompt_with_default("Show", volume_name))

        while True:
            try:
                season = int(input("Season: ").strip())
                if season > 0:
                    break
            except ValueError:
                pass
            print("Please enter a positive integer.", file=sys.stderr)

        while True:
            try:
                first_ep = int(input("First episode number: ").strip())
                if first_ep > 0:
                    break
            except ValueError:
                pass
            print("Please enter a positive integer.", file=sys.stderr)

        episodes = list(range(first_ep, first_ep + len(selected_indexes)))
        ep_preview = ", ".join(f"S{season:02d}E{ep:02d}" for ep in episodes)
        print(f"  → titles will be {ep_preview}")

        if not prompts.prompt_yes_no("Confirm?"):
            print("Aborted.")
            sys.exit(0)

        movie = None
    else:
        show = None
        season = None
        episodes = None
        movie = mungefilename(prompts.prompt_with_default("Movie title", volume_name))

    # ---- Step 4: staging dir confirm, create batch row, and rip ---------------
    staging_dir = pathlib.Path(config.STAGING_ROOT) / (show if is_tv else movie)  # type: ignore[arg-type]

    # Open DB and create the ingest_batches row now so its id becomes part of
    # the raw dir path.  An abort after this point leaves an orphaned batch row;
    # that is acceptable — the row carries no job data and no real-data exists yet.
    conn = db.connect()
    db.init_db(conn)
    batch_id = _create_ingest_batch(
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

    raw_dir.mkdir(parents=True, exist_ok=True)

    failed_idxs = makemkv.run_rips(
        disc=0, minlength=0, outdir=str(raw_dir), indexes=selected_indexes
    )

    surviving_indexes = list(selected_indexes)
    for failed_idx in failed_idxs:
        print(
            f"\nTitle {failed_idx} failed. "
            "Abort the whole ingest, or skip and continue? [a/s]"
        )
        while True:
            c = input("> ").strip().lower()
            if c == "a":
                print("Aborting.", file=sys.stderr)
                sys.exit(1)
            if c == "s":
                surviving_indexes.remove(failed_idx)
                break
            print("Please enter 'a' to abort or 's' to skip.", file=sys.stderr)

    if not surviving_indexes:
        print("No titles survived. Aborting.", file=sys.stderr)
        sys.exit(1)

    surviving_episodes: list[int] | None = None
    if is_tv:
        surviving_episodes = [
            episodes[selected_indexes.index(idx)]  # type: ignore[index]
            for idx in surviving_indexes
        ]

    # ---- Step 5: probe first ripped file ---------------------------------------
    first_source = raw_dir / f"title_t{surviving_indexes[0]:02d}.mkv"
    print(f"\nProbing {first_source.name} ...")

    try:
        probe_data = probe.probe(str(first_source))
        stream_summary = probe.summarize(probe_data)
    except Exception as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_stream_tables(stream_summary)

    # Warn if later files differ (best-effort: just check file count variation)
    if len(surviving_indexes) > 1:
        second_source = raw_dir / f"title_t{surviving_indexes[1]:02d}.mkv"
        if second_source.exists():
            try:
                probe2 = probe.summarize(probe.probe(str(second_source)))
                if len(probe2.audio) != len(stream_summary.audio) or len(
                    probe2.subtitle
                ) != len(stream_summary.subtitle):
                    print(
                        "\nWarning: subsequent titles differ in track layout.",
                        file=sys.stderr,
                    )
            except Exception:
                pass

    # ---- Step 6: encoding config -----------------------------------------------
    print()

    # a. Audio track selection
    num_audio = len(stream_summary.audio)
    if num_audio == 0:
        print("Warning: no audio tracks found in probe.", file=sys.stderr)
        audio_track_indexes: list[int] = []
    else:
        valid_audio = list(range(1, num_audio + 1))
        audio_range = f"1-{num_audio}" if num_audio > 1 else "1"
        while True:
            raw_audio = input(
                f"Audio tracks ({audio_range}, comma-separated, 'all') [1]: "
            ).strip()
            if not raw_audio:
                audio_track_indexes = [1]
                break
            parsed_audio = makemkv.parse_index_spec(raw_audio)
            if parsed_audio is None:
                audio_track_indexes = valid_audio
                break
            invalid_a = [i for i in parsed_audio if i not in valid_audio]
            if invalid_a:
                print(
                    f"Invalid audio track indexes: {invalid_a}. Valid: {valid_audio}",
                    file=sys.stderr,
                )
                continue
            audio_track_indexes = parsed_audio
            break

    # b. Per-track transcode decisions
    audio_tracks: list[AudioTrack] = []
    for track_num in audio_track_indexes:
        stream = stream_summary.audio[track_num - 1]
        audio_tracks.append(prompts.prompt_audio_track_decision(stream, track_num))

    if not audio_tracks:
        audio_tracks = [AudioTrack(track=1)]

    # c. Subtitle track selection
    num_subs = len(stream_summary.subtitle)
    subtitle_tracks: list[int] = []
    if num_subs > 0:
        sub_range = f"1-{num_subs}" if num_subs > 1 else "1"
        raw_sub = input(
            f"Subtitle tracks ({sub_range}, comma-separated, 'none') [none]: "
        ).strip()
        if raw_sub and raw_sub.lower() != "none":
            parsed_sub = makemkv.parse_index_spec(raw_sub)
            if parsed_sub is None:
                subtitle_tracks = list(range(1, num_subs + 1))
            else:
                subtitle_tracks = parsed_sub

    # d. CRF, preset, decomb
    crf_str = prompts.prompt_with_default("Quality (CRF)", str(config.DEFAULT_QUALITY))
    try:
        crf = int(crf_str)
    except ValueError:
        print(
            f"Invalid CRF {crf_str!r}, using {config.DEFAULT_QUALITY}.",
            file=sys.stderr,
        )
        crf = config.DEFAULT_QUALITY

    preset = prompts.prompt_with_default("Preset", config.DEFAULT_PRESET)

    interlaced = any(
        v.field_order not in ("progressive", "", "unknown")
        for v in stream_summary.video
    )
    decomb = False
    if interlaced:
        field_label = (
            stream_summary.video[0].field_order
            if stream_summary.video
            else "interlaced"
        )
        decomb = prompts.prompt_yes_no(
            f"Decomb? Source is {field_label}.", default=False
        )

    hb_args = HandbrakeArgs(
        encoder=config.DEFAULT_ENCODER,
        quality=crf,
        preset=preset,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        decomb=decomb,
    )

    # ---- Step 7: preview, edit, confirm ----------------------------------------
    jobs_plan = _build_jobs_plan(
        is_tv=is_tv,
        show=show,
        season=season,
        episodes=surviving_episodes,
        movie=movie,
        staging_dir=staging_dir,
        raw_dir=raw_dir,
        surviving_indexes=surviving_indexes,
        hb_args=hb_args,
    )

    while True:
        n = len(jobs_plan)
        preview = build_preview(jobs_plan)
        decision = prompts.confirm_or_edit(preview + f"\n\nQueue these {n} job(s)?")
        if decision == "yes":
            break
        if decision == "no":
            print("Aborted.")
            sys.exit(0)
        # edit
        jobs_plan = _edit_plan(jobs_plan)

    # ---- Step 8: insert and exit -----------------------------------------------
    _insert_jobs(conn, batch_id, jobs_plan)

    print(
        f"\nQueued {len(jobs_plan)} job(s) (batch {batch_id}). "
        "Worker will pick them up. Run `rne ls` to check status."
    )
