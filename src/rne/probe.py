from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from rne import config


@dataclass
class VideoStream:
    codec: str
    resolution: str
    fps: str
    field_order: str
    lang: str
    default: bool
    forced: bool


@dataclass
class AudioStream:
    codec: str
    channels: int | None  # channel count from stream.channels; None when absent
    lang: str
    title: str
    default: bool
    forced: bool
    bitrate: int | None  # bits/s; None when not determinable


@dataclass
class SubtitleStream:
    codec: str
    lang: str
    title: str
    default: bool
    forced: bool
    frames: int | None  # NUMBER_OF_FRAMES from MKV statistics tags; None when absent


@dataclass
class StreamSummary:
    video: list[VideoStream]
    audio: list[AudioStream]
    subtitle: list[SubtitleStream]


def layouts_match(a: StreamSummary, b: StreamSummary) -> bool:
    """Return True if a and b share the same track layout.

    Compares audio track count, codec at each audio index, and subtitle
    track count.  Video is intentionally excluded — resolution/codec
    variation between titles on the same disc is expected and harmless for
    encoding config purposes.
    """
    if len(a.audio) != len(b.audio):
        return False
    if len(a.subtitle) != len(b.subtitle):
        return False
    for a_track, b_track in zip(a.audio, b.audio):
        if a_track.codec != b_track.codec:
            return False
    return True


def probe(mkv_path: str) -> dict:
    """Run ffprobe on mkv_path and return parsed JSON."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        mkv_path,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=True, timeout=config.FFPROBE_TIMEOUT
    )
    return json.loads(result.stdout)


def _fr(rate: str) -> str:
    """Convert 'N/D' frame-rate fraction to decimal string."""
    if not rate or rate == "0/0":
        return ""
    try:
        n, d = rate.split("/")
        return f"{int(n) / int(d):.3f}"
    except Exception:
        return rate


def _yn(d: dict, key: str) -> bool:
    return d.get(key) in (1, "1")


def summarize(probe_data: dict) -> StreamSummary:
    """Build a StreamSummary from the dict returned by probe()."""
    streams = probe_data.get("streams", [])

    video_streams: list[VideoStream] = []
    audio_streams: list[AudioStream] = []
    subtitle_streams: list[SubtitleStream] = []

    for s in streams:
        codec_type = s.get("codec_type")
        disp = s.get("disposition", {})
        tags = s.get("tags", {})

        if codec_type == "video":
            w, h = s.get("width", ""), s.get("height", "")
            res = f"{w}x{h}" if w and h else ""
            video_streams.append(
                VideoStream(
                    codec=s.get("codec_name", ""),
                    resolution=res,
                    fps=_fr(s.get("r_frame_rate", "")),
                    field_order=s.get("field_order", ""),
                    lang=tags.get("language", ""),
                    default=_yn(disp, "default"),
                    forced=_yn(disp, "forced"),
                )
            )

        elif codec_type == "audio":
            raw_br = s.get("bit_rate")
            bitrate: int | None = int(raw_br) if raw_br else None
            raw_ch = s.get("channels")
            audio_streams.append(
                AudioStream(
                    codec=s.get("codec_name", ""),
                    channels=int(raw_ch) if raw_ch is not None else None,
                    lang=tags.get("language", ""),
                    title=tags.get("title", ""),
                    default=_yn(disp, "default"),
                    forced=_yn(disp, "forced"),
                    bitrate=bitrate,
                )
            )

        elif codec_type == "subtitle":
            frames_val: int | None = None
            for k, v in tags.items():
                if k.startswith("NUMBER_OF_FRAMES-"):
                    try:
                        frames_val = int(v)
                    except (ValueError, TypeError):
                        pass
                    break
            subtitle_streams.append(
                SubtitleStream(
                    codec=s.get("codec_name", ""),
                    lang=tags.get("language", ""),
                    title=tags.get("title", ""),
                    default=_yn(disp, "default"),
                    forced=_yn(disp, "forced"),
                    frames=frames_val,
                )
            )

    return StreamSummary(
        video=video_streams, audio=audio_streams, subtitle=subtitle_streams
    )
