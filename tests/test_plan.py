"""Tests for rne.plan (shared encode rules) and rne.naming."""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from rne import config, naming, plan
from rne.cli._pipeline import prompt_encoding_config
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack
from rne.probe import AudioStream, StreamSummary, SubtitleStream, VideoStream


def _video(codec="h264", fps="23.976", field="progressive") -> VideoStream:
    return VideoStream(codec=codec, resolution="1920x1080", fps=fps,
                       field_order=field, lang="", default=True, forced=False)


def _audio(codec="ac3", channels=6, **kw) -> AudioStream:
    base = dict(codec=codec, channels=channels, lang="eng", title="",
                default=False, forced=False, bitrate=None)
    base.update(kw)
    return AudioStream(**base)


def _sub(frames=None, **kw) -> SubtitleStream:
    base = dict(codec="hdmv_pgs_subtitle", lang="eng", title="",
                default=False, forced=False, frames=frames)
    base.update(kw)
    return SubtitleStream(**base)


def _summary(video=None, audio=None, subtitle=None) -> StreamSummary:
    return StreamSummary(
        video=[_video()] if video is None else video,
        audio=[_audio()] if audio is None else audio,
        subtitle=[] if subtitle is None else subtitle,
    )


# Sources covering every branch of the default rules.
SOURCES = {
    "bluray-ac3": _summary(),
    "bluray-truehd": _summary(audio=[_audio("truehd", 8), _audio("ac3", 2)]),
    "bluray-no-audio": _summary(audio=[]),
    "bluray-1080i": _summary(video=[_video(field="tt")]),
    "dvd-ntsc": _summary(video=[_video("mpeg2video", "29.970", "tb")]),
    "dvd-pal": _summary(video=[_video("mpeg2video", "25.000", "progressive")]),
    "no-video": _summary(video=[]),
    "with-subs": _summary(subtitle=[_sub(500), _sub(12)]),
}


# ---------------------------------------------------------------------------
# default_args matches pressing Enter at every CLI prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SOURCES))
@pytest.mark.parametrize("is_dvd", [False, True])
def test_default_args_matches_all_enter_prompts(name, is_dvd):
    summary = SOURCES[name]
    with patch("builtins.input", return_value=""):
        prompted = prompt_encoding_config(summary, is_dvd=is_dvd)
    assert plan.default_args(summary, is_dvd=is_dvd) == prompted


# ---------------------------------------------------------------------------
# Source rules
# ---------------------------------------------------------------------------


def test_is_dvd_source_by_codec_or_flag():
    assert plan.is_dvd_source(SOURCES["dvd-ntsc"])
    assert not plan.is_dvd_source(SOURCES["bluray-ac3"])
    assert plan.is_dvd_source(SOURCES["bluray-ac3"], is_dvd=True)
    assert not plan.is_dvd_source(SOURCES["no-video"])


def test_default_preset_by_source():
    assert plan.default_preset(True) == config.DEFAULT_PRESET_DVD
    assert plan.default_preset(False) == config.DEFAULT_PRESET


def test_detelecine_only_for_ntsc_dvd():
    assert plan.detelecine_option(SOURCES["dvd-ntsc"], True) == plan.Toggle(True, "Source is NTSC DVD.")
    assert plan.detelecine_option(SOURCES["dvd-pal"], True) is None
    assert plan.detelecine_option(SOURCES["dvd-ntsc"], False) is None


def test_decomb_rules():
    assert plan.decomb_option(SOURCES["dvd-pal"], True) == plan.Toggle(True, "Source is DVD.")
    assert plan.decomb_option(SOURCES["bluray-1080i"], False) == plan.Toggle(False, "Source is tt.")
    assert plan.decomb_option(SOURCES["bluray-ac3"], False) is None


def test_is_ntsc_tolerates_bad_fps():
    assert not plan.is_ntsc(_summary(video=[_video(fps="")]))


@pytest.mark.parametrize("codec", sorted(config.COPY_FRIENDLY_AUDIO_CODECS))
def test_recommended_audio_copies_friendly_codecs(codec):
    assert plan.recommended_audio(_audio(codec.upper(), 6), 3) == AudioTrack(track=3, codec="copy")


@pytest.mark.parametrize(
    "channels,bitrate", [(1, 96), (2, 192), (6, 640), (8, 640), (None, 192), (4, 640)]
)
def test_recommended_audio_transcodes_to_ac3(channels, bitrate):
    assert plan.recommended_audio(_audio("dts", channels), 2) == AudioTrack(
        track=2, codec="ac3", bitrate=bitrate
    )


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_audio_label_full():
    s = _audio("truehd", 6, bitrate=3_200_000, title="Commentary", default=True)
    assert plan.audio_label(2, s) == '2: truehd 5.1 eng 3.2 Mb/s "Commentary" (default)'


def test_audio_label_minimal():
    s = _audio("ac3", None, lang="", bitrate=192_000)
    assert plan.audio_label(1, s) == "1: ac3 192 kb/s"


def test_subtitle_label_flags_likely_forced():
    assert plan.subtitle_label(2, _sub(12)) == "2: hdmv_pgs_subtitle eng 12 frames (likely forced)"
    assert plan.subtitle_label(1, _sub(800)) == "1: hdmv_pgs_subtitle eng 800 frames"
    assert plan.subtitle_label(3, _sub(None, forced=True)).endswith("(forced)")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_ok():
    s = SOURCES["with-subs"]
    args = HandbrakeArgs(subtitle_tracks=[SubtitleTrack(2, default=True)])
    assert plan.validate(args, s) == []


def test_validate_reports_missing_and_duplicate_tracks():
    s = SOURCES["with-subs"]  # 1 audio, 2 subs
    args = HandbrakeArgs(
        audio_tracks=[AudioTrack(1), AudioTrack(1), AudioTrack(4)],
        subtitle_tracks=[SubtitleTrack(3), SubtitleTrack(1, True), SubtitleTrack(2, True)],
    )
    errors = plan.validate(args, s)
    assert "audio track 1 is listed twice" in errors
    assert "audio track 4 does not exist (source has 1)" in errors
    assert "subtitle track 3 does not exist (source has 2)" in errors
    assert "at most one subtitle track can be the default" in errors


def test_validate_requires_audio():
    assert "at least one audio track is required" in plan.validate(
        HandbrakeArgs(audio_tracks=[]), SOURCES["bluray-ac3"]
    )


def test_with_chapters_copies_settings():
    base = HandbrakeArgs(quality=18, tune="animation", extra_args=["--x"])
    out = plan.with_chapters(base, 3, 5)
    assert (out.chapter_start, out.chapter_end, out.quality, out.tune) == (3, 5, 18, "animation")
    out.extra_args.append("--y")
    assert base.extra_args == ["--x"]


# ---------------------------------------------------------------------------
# Job entries + naming
# ---------------------------------------------------------------------------


def test_tv_job_entry():
    args = HandbrakeArgs()
    job = plan.tv_job(show="Initial D", season=1, episode=5,
                      show_dir=pathlib.Path("/s/Initial D"), source_path="/r/a.mkv", args=args)
    assert job == {
        "label": "S01E05",
        "show": "Initial D",
        "season": 1,
        "episode": 5,
        "movie": None,
        "year": None,
        "tvdb_id": None,
        "source_path": "/r/a.mkv",
        "output_path": "/s/Initial D/Season 01/Initial D - S01E05.mkv",
        "handbrake_args": args,
        "layout_warning": False,
    }


def test_movie_job_entry_with_suffix():
    job = plan.movie_job(movie="Heat", movie_dir=pathlib.Path("/s/Heat"),
                         source_path=pathlib.Path("/r/b.mkv"), args=HandbrakeArgs(), suffix="_t03")
    assert job["output_path"] == "/s/Heat/Heat_t03.mkv"
    assert (job["label"], job["show"], job["movie"], job["source_path"]) == ("Heat", None, "Heat", "/r/b.mkv")


def test_naming_templates():
    root = pathlib.Path("/staging")
    assert naming.title_dir(root, "Show") == pathlib.Path("/staging/Show")
    assert naming.tv_output_path(root / "Show", "Show", 0, 12) == pathlib.Path(
        "/staging/Show/Season 00/Show - S00E12.mkv"
    )
    assert naming.movie_output_path(root / "M", "M") == pathlib.Path("/staging/M/M.mkv")
    assert naming.episode_label(2, 103) == "S02E103"
