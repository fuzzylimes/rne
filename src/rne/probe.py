from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


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
    duration: float | None  # seconds from stream header; None when absent


@dataclass
class StreamSummary:
    video: list[VideoStream]
    audio: list[AudioStream]
    subtitle: list[SubtitleStream]


def probe(mkv_path: str) -> dict:
    """Run ffprobe on mkv_path and return parsed JSON."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        mkv_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
    fmt = probe_data.get("format", {})

    raw_audio = [s for s in streams if s.get("codec_type") == "audio"]
    num_audio = len(raw_audio) or 1

    # Format-level bitrate used as fallback when stream.bit_rate is absent
    fmt_br_str = fmt.get("bit_rate")
    fmt_br = int(fmt_br_str) if fmt_br_str else None

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
            video_streams.append(VideoStream(
                codec=s.get("codec_name", ""),
                resolution=res,
                fps=_fr(s.get("r_frame_rate", "")),
                field_order=s.get("field_order", ""),
                lang=tags.get("language", ""),
                default=_yn(disp, "default"),
                forced=_yn(disp, "forced"),
            ))

        elif codec_type == "audio":
            raw_br = s.get("bit_rate")
            if raw_br:
                bitrate: int | None = int(raw_br)
            elif fmt_br is not None:
                bitrate = fmt_br // num_audio
            else:
                bitrate = None
            raw_ch = s.get("channels")
            audio_streams.append(AudioStream(
                codec=s.get("codec_name", ""),
                channels=int(raw_ch) if raw_ch is not None else None,
                lang=tags.get("language", ""),
                title=tags.get("title", ""),
                default=_yn(disp, "default"),
                forced=_yn(disp, "forced"),
                bitrate=bitrate,
            ))

        elif codec_type == "subtitle":
            dur = s.get("duration")
            subtitle_streams.append(SubtitleStream(
                codec=s.get("codec_name", ""),
                lang=tags.get("language", ""),
                title=tags.get("title", ""),
                default=_yn(disp, "default"),
                forced=_yn(disp, "forced"),
                duration=float(dur) if dur else None,
            ))

    return StreamSummary(video=video_streams, audio=audio_streams, subtitle=subtitle_streams)
