"""Shared pipeline helpers used by both `rne ingest` and `rne queue`."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

from rne import config, probe
from rne.cli import prompts
from rne.makemkv import parse_index_spec
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack

# Characters stripped from user-supplied path components, mirroring .abcde.conf:
#   sed 's/[:><|*/\"'\''?[:cntrl:]]//g'
_MUNGE_RE = re.compile(r"""[:<>|*/\\"'?]|[\x00-\x1f\x7f]""")


def mungefilename(name: str) -> str:
    """Strip characters that are unsafe in filesystem paths."""
    return _MUNGE_RE.sub("", name)


# ---------------------------------------------------------------------------
# Table display
# ---------------------------------------------------------------------------


def _print_table(
    cols: list[str],
    rows: list[dict],
    right_align: set[str] | None = None,
) -> None:
    ra = right_align or set()
    widths = {
        c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0))
        for c in cols
    }
    header = "  ".join(
        f"{c:>{widths[c]}}" if c in ra else f"{c:<{widths[c]}}" for c in cols
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(
            f"{str(row.get(c, '')):>{widths[c]}}" if c in ra
            else f"{str(row.get(c, '')):<{widths[c]}}"
            for c in cols
        ))


def probe_and_display(path: pathlib.Path) -> probe.StreamSummary:
    """Probe path, print stream tables, and return the StreamSummary.

    Calls sys.exit(1) on probe failure.
    """
    print(f"\nProbing {path.name} ...")
    try:
        probe_data = probe.probe(str(path))
        stream_summary = probe.summarize(probe_data)
    except Exception as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print_stream_tables(stream_summary)
    return stream_summary


def print_stream_tables(summary: probe.StreamSummary) -> None:
    if summary.video:
        print("\nVideo:")
        vcols = ["#", "Codec", "Resolution", "FPS",
                 "Field", "Lang", "Def", "Forced"]
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
        acols = ["#", "Codec", "Ch", "Bitrate",
                 "Lang", "Title", "Def", "Forced"]
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
        scols = ["#", "Codec", "Lang", "Title", "Def", "Forced", "Frames"]
        srows = [
            {
                "#": i,
                "Codec": s.codec,
                "Lang": s.lang,
                "Title": s.title,
                "Def": "Y" if s.default else "",
                "Forced": "Y" if s.forced else "",
                "Frames": str(s.frames) if s.frames is not None else "—",
            }
            for i, s in enumerate(summary.subtitle, 1)
        ]
        _print_table(scols, srows, right_align={"Frames"})


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


def _subtitle_summary(subtitle_tracks: list[SubtitleTrack]) -> str:
    parts = []
    for t in subtitle_tracks:
        parts.append(f"{t.track}*" if t.default else str(t.track))
    return "[" + ",".join(parts) + "]"


def build_preview(jobs_plan: list[dict]) -> str:
    """Return preview text for a list of job plan dicts.

    Each dict must have: label, output_path, handbrake_args (HandbrakeArgs).
    Optional key layout_warning=True adds a ⚠ marker to that line.
    Pure function — no I/O.
    """
    lines = ["Preview:"]
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
        out_name = pathlib.Path(job["output_path"]).name
        audio_str = _audio_summary(hb.audio_tracks)
        sub_str = _subtitle_summary(hb.subtitle_tracks)
        details = f"(a={audio_str} s={sub_str} crf={hb.quality} preset={hb.preset}"
        if hb.tune is not None:
            details += f" tune={hb.tune}"
        details += ")"
        warning = "  ⚠ different track layout" if job.get("layout_warning") else ""
        lines.append(f"  {job['label']:<8}  {out_name}  {details}{warning}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mismatch description (pure — tested directly)
# ---------------------------------------------------------------------------


def _describe_mismatch(
    ref: probe.StreamSummary,
    other: probe.StreamSummary,
    label: str,
) -> str:
    """Human-readable description of how other's layout differs from ref."""
    parts: list[str] = []
    if len(other.audio) != len(ref.audio):
        parts.append(
            f"audio track count {len(other.audio)} vs reference {len(ref.audio)}"
        )
    else:
        for i, (ra, oa) in enumerate(zip(ref.audio, other.audio), 1):
            if ra.codec != oa.codec:
                parts.append(f"audio track {i} is {oa.codec} instead of {ra.codec}")
    if len(other.subtitle) != len(ref.subtitle):
        parts.append(
            f"subtitle track count {len(other.subtitle)} vs reference {len(ref.subtitle)}"
        )
    desc = "; ".join(parts) if parts else "unknown difference"
    return f"{label} differs: {desc}."


# ---------------------------------------------------------------------------
# Metadata prompts
# ---------------------------------------------------------------------------


def prompt_metadata(
    name_hint: str,
    num_files: int,
) -> tuple[bool, str | None, int | None, int | None, str | None]:
    """Prompt for content type and naming.

    Returns (is_tv, show, season, first_episode, movie).
    Calls sys.exit(0) if the user confirms TV episodes but then aborts.
    """
    print("What type of content is this?")
    print("  [1] TV episodes")
    print("  [2] Movie")
    while True:
        choice = input("> ").strip()
        if choice in ("1", "2"):
            break
        print("Please enter 1 or 2.", file=sys.stderr)

    is_tv = choice == "1"

    if is_tv:
        show = mungefilename(prompts.prompt_with_default("Show", name_hint))

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

        episodes = list(range(first_ep, first_ep + num_files))
        ep_preview = ", ".join(f"S{season:02d}E{ep:02d}" for ep in episodes)
        print(f"  → titles will be {ep_preview}")

        if not prompts.prompt_yes_no("Confirm?"):
            print("Aborted.")
            sys.exit(0)

        return True, show, season, first_ep, None
    else:
        movie = mungefilename(prompts.prompt_with_default("Movie title", name_hint))
        return False, None, None, None, movie


# ---------------------------------------------------------------------------
# Encoding configuration questionnaire
# ---------------------------------------------------------------------------


def _parse_audio_selection(raw: str, valid: list[int]) -> list[int]:
    """Parse user audio-track input preserving selection order.

    Returns valid (source order) for 'all'.
    Raises ValueError for duplicate or out-of-range indexes.
    """
    if raw.strip().lower() == "all":
        return list(valid)
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            raise ValueError(f"invalid track index: {part!r}")
        if idx in result:
            raise ValueError(f"duplicate track index: {idx}")
        if idx not in valid:
            raise ValueError(
                f"track index {idx} out of range; valid: {valid}"
            )
        result.append(idx)
    if not result:
        raise ValueError("no track indexes given")
    return result


def prompt_encoding_config(stream_summary: probe.StreamSummary) -> HandbrakeArgs:
    """Run the encoding config prompts and return a HandbrakeArgs.

    Reuses the same prompts as the ingest flow: audio selection, per-track
    transcode decisions, subtitle selection, CRF, preset, animation tune,
    and decomb for interlaced sources.
    """
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
            try:
                audio_track_indexes = _parse_audio_selection(raw_audio, valid_audio)
                break
            except ValueError as exc:
                print(str(exc), file=sys.stderr)

    # b. Per-track transcode decisions
    audio_tracks: list[AudioTrack] = []
    for track_num in audio_track_indexes:
        stream = stream_summary.audio[track_num - 1]
        audio_tracks.append(prompts.prompt_audio_track_decision(stream, track_num))

    if not audio_tracks:
        audio_tracks = [AudioTrack(track=1)]

    # c. Subtitle track selection
    num_subs = len(stream_summary.subtitle)
    subtitle_track_indexes: list[int] = []
    if num_subs > 0:
        sub_range = f"1-{num_subs}" if num_subs > 1 else "1"
        raw_sub = input(
            f"Subtitle tracks ({sub_range}, comma-separated, 'none') [none]: "
        ).strip()
        if raw_sub and raw_sub.lower() != "none":
            parsed_sub = parse_index_spec(raw_sub)
            if parsed_sub is None:
                subtitle_track_indexes = list(range(1, num_subs + 1))
            else:
                subtitle_track_indexes = parsed_sub

    # c2. Default subtitle selection
    subtitle_tracks: list[SubtitleTrack] = []
    if subtitle_track_indexes:
        subtitle_tracks = [SubtitleTrack(track=n) for n in subtitle_track_indexes]
        n_sel = len(subtitle_track_indexes)
        if n_sel == 1:
            def_prompt = "Default subtitle track? (1 or 0 for none) [0]: "
        else:
            def_prompt = (
                f"Default subtitle track? (1-{n_sel}, or 0 for none) [0]: "
            )
        while True:
            raw_def = input(def_prompt).strip()
            if not raw_def or raw_def == "0":
                break
            try:
                sel = int(raw_def)
                if 1 <= sel <= n_sel:
                    source_track = subtitle_track_indexes[sel - 1]
                    subtitle_tracks[sel - 1] = SubtitleTrack(
                        track=source_track, default=True
                    )
                    if source_track != sel:
                        print(f"Marked source track {source_track} as default.")
                    break
                print(
                    f"Please enter a number between 0 and {n_sel}.",
                    file=sys.stderr,
                )
            except ValueError:
                print("Please enter a number.", file=sys.stderr)

    # d. CRF, preset, animation tune, decomb
    crf_str = prompts.prompt_with_default(
        "Quality (CRF)", str(config.DEFAULT_QUALITY))
    try:
        crf = int(crf_str)
    except ValueError:
        print(
            f"Invalid CRF {crf_str!r}, using {config.DEFAULT_QUALITY}.",
            file=sys.stderr,
        )
        crf = config.DEFAULT_QUALITY

    preset = prompts.prompt_with_default("Preset", config.DEFAULT_PRESET)

    tune: str | None = config.DEFAULT_TUNE
    if prompts.prompt_yes_no("Animation source?", default=False):
        tune = "animation"

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

    return HandbrakeArgs(
        encoder=config.DEFAULT_ENCODER,
        quality=crf,
        preset=preset,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        decomb=decomb,
        tune=tune,
    )


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


def edit_plan(jobs_plan: list[dict]) -> list[dict]:
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
# Preview loop with mismatch detection
# ---------------------------------------------------------------------------


def preview_and_confirm(
    jobs_plan: list[dict],
    ref_summary: probe.StreamSummary,
    remaining_file_paths: list[pathlib.Path],
) -> list[dict]:
    """Probe remaining files, flag mismatches, run the confirm loop.

    remaining_file_paths corresponds to jobs_plan[1:] — the files whose
    layout should be compared against ref_summary (the first file's probe).

    Returns the final (possibly filtered) jobs_plan.
    Calls sys.exit on abort.
    """
    mismatch_details: list[str] = []
    for pos, file_path in enumerate(remaining_file_paths, 1):
        try:
            other_summary = probe.summarize(probe.probe(str(file_path)))
            if not probe.layouts_match(ref_summary, other_summary):
                jobs_plan[pos]["layout_warning"] = True
                mismatch_details.append(
                    _describe_mismatch(
                        ref_summary, other_summary, jobs_plan[pos]["label"]
                    )
                )
        except Exception as exc:
            print(f"  Warning: could not probe {file_path.name}: {exc}", file=sys.stderr)
            jobs_plan[pos]["layout_warning"] = True

    while True:
        n = len(jobs_plan)
        preview = build_preview(jobs_plan)

        if mismatch_details:
            mismatch_text = "\n".join(mismatch_details)
            print(f"{preview}\n\n{mismatch_text}")
            resp = input(
                f"\nQueue these {n} job(s)? [Y/n/edit/skip-mismatched]: "
            ).strip().lower()
            if resp in ("", "y", "yes"):
                break
            if resp in ("n", "no"):
                print("Aborted.")
                sys.exit(0)
            if resp in ("e", "edit"):
                jobs_plan = edit_plan(jobs_plan)
                continue
            if resp in ("skip-mismatched", "skip"):
                jobs_plan = [j for j in jobs_plan if not j.get("layout_warning")]
                if not jobs_plan:
                    print("No matching titles to queue. Aborting.", file=sys.stderr)
                    sys.exit(1)
                mismatch_details = []
                break
            print("Please enter Y, n, edit, or skip-mismatched.", file=sys.stderr)
        else:
            decision = prompts.confirm_or_edit(
                preview + f"\n\nQueue these {n} job(s)?"
            )
            if decision == "yes":
                break
            if decision == "no":
                print("Aborted.")
                sys.exit(0)
            jobs_plan = edit_plan(jobs_plan)

    return jobs_plan


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def create_batch_row(
    conn,
    *,
    is_tv: bool,
    show: str | None,
    movie: str | None,
    season: int | None,
    notes: str,
    label_suffix: str = "",
) -> int:
    """Insert an ingest_batches row and return its id."""
    base_label = f"{show} S{season:02d}" if is_tv else str(movie)
    label = base_label + label_suffix
    cur = conn.execute(
        "INSERT INTO ingest_batches (label, show, movie, notes) VALUES (?, ?, ?, ?)",
        (label, show, movie, notes),
    )
    conn.commit()
    return cur.lastrowid


def insert_jobs(conn, batch_id: int, jobs_plan: list[dict]) -> None:
    """Insert job rows for an already-created ingest batch."""
    for job in jobs_plan:
        hb: HandbrakeArgs = job["handbrake_args"]
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
