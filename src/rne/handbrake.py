from __future__ import annotations

from rne import config
from rne.models import HandbrakeArgs

_KNOWN_ENCODERS = frozenset(
    {
        "x264",
        "x264_10b",
        "x265",
        "x265_10b",
        "x265_12b",
        "mpeg4",
        "mpeg2",
        "VP8",
        "VP9",
        "theora",
        "av1",
    }
)


def build_command(
    source_path: str,
    output_path: str,
    args: HandbrakeArgs,
) -> list[str]:
    if not args.audio_tracks:
        raise ValueError("audio_tracks must not be empty")
    if args.quality < 0:
        raise ValueError(f"quality must be non-negative, got {args.quality}")
    if args.encoder not in _KNOWN_ENCODERS:
        raise ValueError(f"unknown encoder: {args.encoder!r}")
    default_subs = [t for t in args.subtitle_tracks if t.default]
    if len(default_subs) > 1:
        raise ValueError(
            f"at most one subtitle track may be default, got {len(default_subs)}"
        )

    tracks_a: list[str] = []
    tracks_e: list[str] = []
    tracks_b: list[str] = []
    any_real_bitrate: bool = False
    for t in args.audio_tracks:
        tracks_a.append(str(t.track))
        tracks_e.append(t.codec)
        # Only append if not copy
        if t.codec == "copy":
            tracks_b.append("")
        else:
            tracks_b.append(str(t.bitrate))
            any_real_bitrate = True

    cmd = list(config.HANDBRAKE_PREFIX) + [
        "-i",
        source_path,
        "-o",
        output_path,
        "-f", 
        "av_mkv",
        "--encoder",
        args.encoder,
        "--quality",
        str(args.quality),
        "--encoder-preset",
        args.preset,
        "-a",
        ",".join(tracks_a),
        "-E",
        ",".join(tracks_e),
    ]

    if any_real_bitrate:
        cmd += ["-B", ",".join(tracks_b)]

    if args.subtitle_tracks:
        cmd += ["-s", ",".join(str(t.track) for t in args.subtitle_tracks)]
        for i, t in enumerate(args.subtitle_tracks, 1):
            if t.default:
                cmd += ["--subtitle-default", str(i)]
                break

    if args.chapter_start is not None and args.chapter_end is not None:
        cmd += ["--chapters", f"{args.chapter_start}-{args.chapter_end}"]

    cmd += ["--markers", "--align-av"]

    if args.tune is not None:
        cmd += ["--encoder-tune", args.tune]

    if args.detelecine:
        cmd += ["--detelecine"]

    if args.decomb:
        cmd += ["--decomb"]

    cmd += list(args.extra_args)

    return cmd
