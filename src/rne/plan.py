"""Encode-settings rules shared by every front end.

The CLI prompts, the TUI, the dashboard edit form and the enqueue API all need
the same answers to "what are the choices for this file?" and "what is the
default?". Those answers live here so the front ends only differ in how they
ask. Pure: no prompting, no subprocess, no DB.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from rne import config, naming
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack
from rne.probe import AudioStream, StreamSummary, SubtitleStream

# x265 presets, fastest to slowest, as accepted by HandBrake's --encoder-preset.
ENCODER_PRESETS: tuple[str, ...] = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
)

# x265 tunes accepted by --encoder-tune. None means "no tune flag".
ENCODER_TUNES: tuple[str | None, ...] = (
    None, "animation", "grain", "psnr", "ssim", "fastdecode", "zerolatency",
)

# HandBrake audio encoder names offered in pickers. "copy" passes through.
AUDIO_CODECS: tuple[str, ...] = ("copy", "ac3", "eac3", "av_aac", "opus")

# Bitrates offered in pickers (kbps). AC3 tops out at 640.
AUDIO_BITRATES: tuple[int, ...] = (96, 128, 160, 192, 224, 256, 320, 384, 448, 640)

# Subtitle tracks with fewer cues than this are probably forced-only.
FORCED_FRAMES_HINT = 100

_NTSC_FPS_RANGE = (28.0, 31.0)
_PROGRESSIVE_FIELD_ORDERS = ("progressive", "", "unknown")


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------


def is_dvd_source(summary: StreamSummary, is_dvd: bool = False) -> bool:
    """DVD if forced by --dvd, or the first video stream is MPEG-2."""
    return is_dvd or (bool(summary.video) and summary.video[0].codec == "mpeg2video")


def is_ntsc(summary: StreamSummary) -> bool:
    if not summary.video:
        return False
    try:
        fps = float(summary.video[0].fps)
    except (ValueError, TypeError):
        fps = 0.0
    return _NTSC_FPS_RANGE[0] <= fps <= _NTSC_FPS_RANGE[1]


def is_interlaced(summary: StreamSummary) -> bool:
    return any(v.field_order not in _PROGRESSIVE_FIELD_ORDERS for v in summary.video)


def default_preset(dvd_source: bool) -> str:
    """DVDs encode fast enough that "slow" buys little."""
    return config.DEFAULT_PRESET_DVD if dvd_source else config.DEFAULT_PRESET


@dataclass(frozen=True)
class Toggle:
    """A yes/no filter option that applies to this source."""

    default: bool
    reason: str  # shown next to the question, e.g. "Source is NTSC DVD."


def detelecine_option(summary: StreamSummary, dvd_source: bool) -> Toggle | None:
    """Detelecine is only offered for NTSC DVDs, defaulting on."""
    if dvd_source and is_ntsc(summary):
        return Toggle(default=True, reason="Source is NTSC DVD.")
    return None


def decomb_option(summary: StreamSummary, dvd_source: bool) -> Toggle | None:
    """Always offered for DVDs (default on); for others only when interlaced (default off)."""
    if dvd_source:
        return Toggle(default=True, reason="Source is DVD.")
    if is_interlaced(summary):
        field = summary.video[0].field_order if summary.video else "interlaced"
        return Toggle(default=False, reason=f"Source is {field}.")
    return None


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def is_copy_friendly(stream: AudioStream) -> bool:
    return stream.codec.lower() in config.COPY_FRIENDLY_AUDIO_CODECS


def recommended_ac3_bitrate(stream: AudioStream) -> int:
    # Unknown channel count is treated as stereo.
    channels = stream.channels if stream.channels is not None else 2
    return config.AC3_BITRATE_BY_CHANNELS.get(channels, 640)


def recommended_audio(stream: AudioStream, track: int) -> AudioTrack:
    """Copy universally playable codecs; transcode everything else to AC3."""
    if is_copy_friendly(stream):
        return AudioTrack(track=track, codec="copy")
    return AudioTrack(track=track, codec="ac3", bitrate=recommended_ac3_bitrate(stream))


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_args(summary: StreamSummary, is_dvd: bool = False) -> HandbrakeArgs:
    """The settings you get by pressing Enter at every prompt."""
    dvd_source = is_dvd_source(summary, is_dvd)
    if summary.audio:
        audio = [recommended_audio(summary.audio[0], 1)]
    else:
        audio = [AudioTrack(track=1)]
    detelecine = detelecine_option(summary, dvd_source)
    decomb = decomb_option(summary, dvd_source)
    return HandbrakeArgs(
        encoder=config.DEFAULT_ENCODER,
        quality=config.DEFAULT_QUALITY,
        preset=default_preset(dvd_source),
        audio_tracks=audio,
        subtitle_tracks=[],
        detelecine=detelecine.default if detelecine else False,
        decomb=decomb.default if decomb else False,
        tune=config.DEFAULT_TUNE,
    )


# ---------------------------------------------------------------------------
# Labels for pickers
# ---------------------------------------------------------------------------


_CHANNEL_LAYOUTS = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}


def _channels_label(channels: int) -> str:
    return _CHANNEL_LAYOUTS.get(channels, f"{channels}ch")


def audio_label(track: int, stream: AudioStream) -> str:
    """e.g. '2: truehd 5.1 eng 3.2 Mb/s "Commentary"'."""
    parts = [f"{track}:", stream.codec]
    if stream.channels:
        parts.append(_channels_label(stream.channels))
    if stream.lang:
        parts.append(stream.lang)
    if stream.bitrate:
        kbps = stream.bitrate // 1000
        parts.append(f"{kbps / 1000:.1f} Mb/s" if kbps >= 1000 else f"{kbps} kb/s")
    if stream.title:
        parts.append(f'"{stream.title}"')
    if stream.default:
        parts.append("(default)")
    return " ".join(parts)


def subtitle_label(track: int, stream: SubtitleStream) -> str:
    """e.g. '3: hdmv_pgs_subtitle eng 24 frames (likely forced)'."""
    parts = [f"{track}:", stream.codec]
    if stream.lang:
        parts.append(stream.lang)
    if stream.frames is not None:
        parts.append(f"{stream.frames} frames")
    if stream.title:
        parts.append(f'"{stream.title}"')
    if stream.forced:
        parts.append("(forced)")
    elif stream.frames is not None and stream.frames < FORCED_FRAMES_HINT:
        parts.append("(likely forced)")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Validation against the source
# ---------------------------------------------------------------------------


def validate(args: HandbrakeArgs, summary: StreamSummary | None) -> list[str]:
    """Problems that would make this encode wrong for this source. Empty = OK.

    The dataclasses already enforce per-field shape (bitrate vs copy, positive
    track numbers); this checks the settings against the file's real tracks.
    summary None (source unreadable) skips the checks that need the file.
    """
    errors: list[str] = []
    if not args.audio_tracks:
        errors.append("at least one audio track is required")
    seen_audio: set[int] = set()
    for t in args.audio_tracks:
        if summary is not None and t.track > len(summary.audio):
            errors.append(f"audio track {t.track} does not exist (source has {len(summary.audio)})")
        if t.track in seen_audio:
            errors.append(f"audio track {t.track} is listed twice")
        seen_audio.add(t.track)
    seen_subs: set[int] = set()
    for s in args.subtitle_tracks:
        if summary is not None and s.track > len(summary.subtitle):
            errors.append(
                f"subtitle track {s.track} does not exist (source has {len(summary.subtitle)})"
            )
        if s.track in seen_subs:
            errors.append(f"subtitle track {s.track} is listed twice")
        seen_subs.add(s.track)
    if sum(1 for s in args.subtitle_tracks if s.default) > 1:
        errors.append("at most one subtitle track can be the default")
    if args.quality < 0:
        errors.append("quality must be 0 or higher")
    return errors


def with_chapters(args: HandbrakeArgs, start: int | None, end: int | None) -> HandbrakeArgs:
    """Copy of args encoding only chapters start..end (both None = whole file)."""
    return HandbrakeArgs(
        encoder=args.encoder,
        quality=args.quality,
        preset=args.preset,
        audio_tracks=list(args.audio_tracks),
        subtitle_tracks=[SubtitleTrack(track=s.track, default=s.default) for s in args.subtitle_tracks],
        detelecine=args.detelecine,
        decomb=args.decomb,
        tune=args.tune,
        extra_args=list(args.extra_args),
        chapter_start=start,
        chapter_end=end,
    )


# ---------------------------------------------------------------------------
# Job plan entries
#
# A job plan is a list of dicts, one per job, with the keys below. It is what
# the preview shows, what `edit` round-trips through $EDITOR, and what
# jobs.insert_jobs writes. Optional keys: layout_warning (bool),
# source_summary (StreamSummary), source_chapters (list[Chapter]).
# ---------------------------------------------------------------------------


def tv_job(
    *,
    show: str,
    season: int,
    episode: int,
    show_dir: pathlib.Path,
    source_path: pathlib.Path | str,
    args: HandbrakeArgs,
) -> dict:
    return {
        "label": naming.episode_label(season, episode),
        "show": show,
        "season": season,
        "episode": episode,
        "movie": None,
        "source_path": str(source_path),
        "output_path": str(naming.tv_output_path(show_dir, show, season, episode)),
        "handbrake_args": args,
        "layout_warning": False,
    }


def movie_job(
    *,
    movie: str,
    movie_dir: pathlib.Path,
    source_path: pathlib.Path | str,
    args: HandbrakeArgs,
    suffix: str = "",
) -> dict:
    return {
        "label": movie,
        "show": None,
        "season": None,
        "episode": None,
        "movie": movie,
        "source_path": str(source_path),
        "output_path": str(naming.movie_output_path(movie_dir, movie, suffix)),
        "handbrake_args": args,
        "layout_warning": False,
    }


# ---------------------------------------------------------------------------
# Editable track rows
#
# Every track in the source becomes a row a form can show. Included rows carry
# the job's choices; the others carry the recommendation, so ticking one gives
# a sensible codec straight away. Used by the TUI and the dashboard form.
# ---------------------------------------------------------------------------


@dataclass
class AudioRow:
    track: int  # 1-based source stream index
    label: str
    include: bool
    codec: str  # "copy" or a HandBrake audio encoder
    bitrate: int | None  # kbps when transcoding
    recommended_bitrate: int


@dataclass
class SubtitleRow:
    track: int
    label: str
    include: bool
    default: bool


def _missing_label(track: int, source_known: bool = True) -> str:
    return f"{track}: not in source file" if source_known else f"{track}: (source not readable)"


_NO_SOURCE = StreamSummary(video=[], audio=[], subtitle=[])


def audio_rows(summary: StreamSummary | None, args: HandbrakeArgs) -> list[AudioRow]:
    """Included tracks first, in playback order; then the rest in source order.

    summary None (source unreadable): only the job's own tracks.
    """
    known = summary is not None
    summary = summary or _NO_SOURCE
    rows: list[AudioRow] = []
    chosen = {t.track for t in args.audio_tracks}
    for t in args.audio_tracks:
        stream = summary.audio[t.track - 1] if t.track <= len(summary.audio) else None
        rows.append(AudioRow(
            track=t.track,
            label=audio_label(t.track, stream) if stream else _missing_label(t.track, known),
            include=True,
            codec=t.codec,
            bitrate=t.bitrate,
            recommended_bitrate=recommended_ac3_bitrate(stream) if stream else 640,
        ))
    for n, stream in enumerate(summary.audio, 1):
        if n in chosen:
            continue
        rec = recommended_audio(stream, n)
        rows.append(AudioRow(
            track=n,
            label=audio_label(n, stream),
            include=False,
            codec=rec.codec,
            bitrate=rec.bitrate,
            recommended_bitrate=recommended_ac3_bitrate(stream),
        ))
    return rows


def subtitle_rows(summary: StreamSummary | None, args: HandbrakeArgs) -> list[SubtitleRow]:
    """Source order, then any chosen tracks the source doesn't have."""
    known = summary is not None
    summary = summary or _NO_SOURCE
    chosen = {s.track: s for s in args.subtitle_tracks}
    rows = [
        SubtitleRow(
            track=n,
            label=subtitle_label(n, stream),
            include=n in chosen,
            default=n in chosen and chosen[n].default,
        )
        for n, stream in enumerate(summary.subtitle, 1)
    ]
    for track, s in chosen.items():
        if track > len(summary.subtitle):
            rows.append(SubtitleRow(track, _missing_label(track, known), True, s.default))
    return rows


def args_from_rows(
    base: HandbrakeArgs,
    audio: list[AudioRow],
    subtitles: list[SubtitleRow],
    *,
    quality: int,
    preset: str,
    tune: str | None,
    detelecine: bool,
    decomb: bool,
) -> HandbrakeArgs:
    """Build settings from form rows. Keeps base's encoder, extra args and chapters.

    Raises ValueError if a row is inconsistent (e.g. a non-positive bitrate).
    """
    audio_tracks = [
        AudioTrack(track=r.track, codec="copy")
        if r.codec == "copy"
        else AudioTrack(track=r.track, codec=r.codec, bitrate=r.bitrate or r.recommended_bitrate)
        for r in audio
        if r.include
    ]
    subtitle_tracks = [
        SubtitleTrack(track=r.track, default=r.default) for r in subtitles if r.include
    ]
    return HandbrakeArgs(
        encoder=base.encoder,
        quality=quality,
        preset=preset,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        detelecine=detelecine,
        decomb=decomb,
        tune=tune,
        extra_args=list(base.extra_args),
        chapter_start=base.chapter_start,
        chapter_end=base.chapter_end,
    )
