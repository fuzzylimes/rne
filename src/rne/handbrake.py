from __future__ import annotations

from rne import config
from rne.models import HandbrakeArgs

_KNOWN_ENCODERS = frozenset({
    "x264", "x264_10b",
    "x265", "x265_10b", "x265_12b",
    "mpeg4", "mpeg2",
    "VP8", "VP9",
    "theora",
    "av1",
})


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

    cmd = list(config.HANDBRAKE_PREFIX) + [
        "-i", source_path,
        "-o", output_path,
        "--encoder", args.encoder,
        "--quality", str(args.quality),
        "--encoder-preset", args.preset,
        "-a", ",".join(str(t) for t in args.audio_tracks),
        "--aencoder", ",".join([args.audio_codec] * len(args.audio_tracks)),
    ]

    if args.subtitle_tracks:
        cmd += ["-s", ",".join(str(t) for t in args.subtitle_tracks)]

    cmd += ["--markers", "--align-av"]

    if args.decomb:
        cmd += ["--decomb"]

    cmd += list(args.extra_args)

    return cmd
