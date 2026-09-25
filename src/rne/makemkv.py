from __future__ import annotations

import csv
import io
import pathlib
import re
import subprocess
import sys

# TINFO codes (title-level metadata)
T_CHAPTERS = 8
T_DURATION = 9
T_SIZE = 10
T_SOURCE = 16

# SINFO codes (stream-level metadata)
S_TYPE = 1
S_NAME = 2
S_LANG_CODE = 3
S_CODEC = 6
S_CHANNELS = 14
S_RESOLUTION = 19
S_FPS = 21

# CINFO codes (disc-level metadata)
C_VOLUME_NAME = 2


def parse_line(line: str) -> tuple[str, list[str]] | None:
    if ":" not in line:
        return None
    prefix, rest = line.split(":", 1)
    try:
        fields = next(csv.reader(io.StringIO(rest)))
    except (csv.Error, StopIteration):
        return None
    return prefix, fields


def parse_info(output: str) -> tuple[dict, dict]:
    """Parse makemkvcon -r output. Returns (disc_info, titles)."""
    titles: dict = {}
    disc_info: dict = {}
    for line in output.splitlines():
        parsed = parse_line(line)
        if not parsed:
            continue
        prefix, f = parsed
        if prefix == "CINFO" and len(f) >= 3:
            disc_info[int(f[0])] = f[2]
        elif prefix == "TINFO" and len(f) >= 4:
            tid = int(f[0])
            titles.setdefault(tid, {"info": {}, "streams": {}})
            titles[tid]["info"][int(f[1])] = f[3]
        elif prefix == "SINFO" and len(f) >= 5:
            tid, sid = int(f[0]), int(f[1])
            titles.setdefault(tid, {"info": {}, "streams": {}})
            titles[tid]["streams"].setdefault(sid, {})
            titles[tid]["streams"][sid][int(f[2])] = f[4]
    return disc_info, titles


def summarize(tid: int, title: dict) -> dict:
    """Summarize a single title as a flat dict for display."""
    info, streams = title["info"], title["streams"]
    video = audio = None
    for sid in sorted(streams):
        s = streams[sid]
        stype = s.get(S_TYPE, "")
        if stype == "Video" and video is None:
            video = s
        elif stype == "Audio" and audio is None:
            audio = s

    fps = (video or {}).get(S_FPS, "")
    if "(" in fps:
        fps = fps.split("(")[0].strip()

    if audio:
        audio_str = " ".join(
            filter(
                None,
                [
                    audio.get(S_CODEC, ""),
                    audio.get(S_NAME, ""),
                    f"[{audio.get(S_LANG_CODE, '')}]" if audio.get(S_LANG_CODE) else "",
                ],
            )
        )
    else:
        audio_str = ""

    return {
        "#": tid,
        "Source": info.get(T_SOURCE, ""),
        "Duration": info.get(T_DURATION, ""),
        "Size": info.get(T_SIZE, ""),
        "Ch": info.get(T_CHAPTERS, ""),
        "Resolution": (video or {}).get(S_RESOLUTION, ""),
        "FPS": fps,
        "Audio": audio_str,
    }


_DRIVE_RE = re.compile(r"disc:?(\d+)")


def parse_source(value: str) -> str:
    """Turn a user-facing source into a makemkvcon source spec.

    'disc1' / 'disc:1' -> 'disc:1' (optical drive by index). Anything else is
    taken as a path: a disc backup folder (VIDEO_TS / BDMV, e.g. from
    dvdbackup) -> 'file:/abs/path', or a disc image ending in .iso ->
    'iso:/abs/path'. The drive pattern wins, so a path that happens to be
    named 'disc1' needs a form like './disc1'.

    Raises ValueError if the value is not a drive, an existing directory, or
    an existing .iso file.
    """
    m = _DRIVE_RE.fullmatch(value)
    if m:
        return f"disc:{int(m.group(1))}"
    path = pathlib.Path(value).expanduser()
    if path.is_dir():
        return f"file:{path.resolve()}"
    if path.is_file() and path.suffix.lower() == ".iso":
        return f"iso:{path.resolve()}"
    raise ValueError(
        f"{value!r} is not a drive (disc0, disc:1, ...), an existing directory, "
        "or an existing .iso file"
    )


def parse_index_spec(spec: str) -> list[int] | None:
    """Parse '0-3,5,7' / '0 1 2' / 'all' into a sorted list. Returns None for 'all'."""
    spec = spec.strip().lower()
    if spec == "all":
        return None  # sentinel: caller fills in all valid indexes
    indexes: set[int] = set()
    for part in spec.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            indexes.update(range(int(a), int(b) + 1))
        else:
            indexes.add(int(part))
    return sorted(indexes)


def extract_messages(output: str) -> list[str]:
    """Pull the human-readable text out of makemkvcon -r MSG rows.

    In robot mode makemkvcon writes its messages — including the reason for a
    failure, e.g. an expired beta key or a drive it could not open — to stdout
    as MSG rows, leaving stderr empty. Field 3 is the already-formatted text.
    Consecutive duplicates are collapsed; makemkv repeats itself freely.
    """
    messages: list[str] = []
    for line in output.splitlines():
        parsed = parse_line(line)
        if not parsed:
            continue
        prefix, f = parsed
        if prefix == "MSG" and len(f) >= 4 and f[3] and f[3] != (
            messages[-1] if messages else None
        ):
            messages.append(f[3])
    return messages


def run_info(source: str, minlength: int) -> tuple[dict, dict]:
    """Run makemkvcon info and return (disc_info, titles).

    source is a makemkvcon source spec as returned by parse_source.
    Raises subprocess.CalledProcessError on non-zero exit.
    """
    cmd = ["makemkvcon", "-r", f"--minlength={minlength}", "info", source]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Report messages on any unhappy path. stderr alone is not enough: in robot
    # mode it is usually empty, so relying on it turns a failed info run into a
    # silent non-zero exit with no clue as to why.
    messages = extract_messages(result.stdout)
    if result.returncode != 0:
        for msg in messages:
            print(msg, file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        if not messages and not result.stderr.strip():
            print(
                f"makemkvcon exited {result.returncode} with no output.",
                file=sys.stderr,
            )
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )

    disc_info, titles = parse_info(result.stdout)
    if not titles:
        # Exit 0 but nothing usable — the MSG rows say why (no disc, all titles
        # below minlength, unreadable disc). The caller prints its own summary.
        for msg in messages:
            print(msg, file=sys.stderr)
    return disc_info, titles


class MakemkvError(Exception):
    """Raised when makemkvcon produces unexpected output."""


def rip_and_detect(
    source: str, title_idx: int, raw_dir: pathlib.Path, minlength: int = 900
) -> pathlib.Path:
    """Rip one title and return the path of the newly created MKV.

    Takes a before/after snapshot of raw_dir so the caller never needs to
    predict what filename makemkv chose.  Raises subprocess.CalledProcessError
    on non-zero exit, MakemkvError if exactly one new *.mkv did not appear.

    source and minlength must match the values passed to run_info so title
    indices are consistent between the two commands.
    """
    before = set(raw_dir.glob("*.mkv"))
    cmd = [
        "makemkvcon",
        f"--minlength={minlength}",
        "mkv",
        source,
        str(title_idx),
        str(raw_dir),
    ]
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    after = set(raw_dir.glob("*.mkv"))
    new = after - before
    if len(new) != 1:
        raise MakemkvError(
            f"expected 1 new .mkv after ripping title {title_idx}, "
            f"got {len(new)}: {sorted(str(p) for p in new)}"
        )
    return next(iter(new))
