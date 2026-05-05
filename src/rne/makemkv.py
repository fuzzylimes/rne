from __future__ import annotations

import csv
import io
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
        audio_str = " ".join(filter(None, [
            audio.get(S_CODEC, ""),
            audio.get(S_NAME, ""),
            f"[{audio.get(S_LANG_CODE, '')}]" if audio.get(S_LANG_CODE) else "",
        ]))
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


def run_info(disc: int, minlength: int) -> tuple[dict, dict]:
    """Run makemkvcon info and return (disc_info, titles).

    Raises subprocess.CalledProcessError on non-zero exit.
    """
    cmd = ["makemkvcon", "-r", f"--minlength={minlength}", "info", f"disc:{disc}"]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return parse_info(result.stdout)


def run_rips(disc: int, minlength: int, outdir: str, indexes: list[int]) -> list[int]:
    """Rip selected titles sequentially. Streams makemkvcon stdout to the terminal.

    Returns a list of failed title indexes (empty on full success).
    """
    failed: list[int] = []
    for i in indexes:
        cmd = ["makemkvcon", f"--minlength={minlength}", "mkv",
               f"disc:{disc}", str(i), outdir]
        print(f"\n$ {' '.join(cmd)}")
        if subprocess.run(cmd).returncode != 0:
            print(f"!! Title {i} failed", file=sys.stderr)
            failed.append(i)
    print()
    return failed
