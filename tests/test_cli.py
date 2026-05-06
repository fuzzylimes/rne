"""Tests for CLI helpers and pure functions."""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from rne.cli.ingest import _audio_summary, _build_jobs_plan, build_preview
from rne.cli.prompts import prompt_audio_track_decision
from rne.models import AudioTrack, HandbrakeArgs
from rne.probe import AudioStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stream(codec: str, channels: int | None = None) -> AudioStream:
    return AudioStream(
        codec=codec,
        channels=channels,
        lang="",
        title="",
        default=False,
        forced=False,
        bitrate=None,
    )


def _job(
    label: str,
    output_path: str,
    audio_tracks: list[AudioTrack],
    subtitle_tracks: list[int] | None = None,
    quality: int = 20,
    preset: str = "slow",
) -> dict:
    return {
        "label": label,
        "show": None,
        "season": None,
        "episode": None,
        "movie": label,
        "source_path": "/tmp/t.mkv",
        "output_path": output_path,
        "handbrake_args": HandbrakeArgs(
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks or [],
            quality=quality,
            preset=preset,
        ),
    }


# ---------------------------------------------------------------------------
# prompt_audio_track_decision — copy-friendly codecs (no prompt)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", ["ac3", "eac3", "aac", "mp3", "opus"])
def test_copy_friendly_no_prompt(codec):
    result = prompt_audio_track_decision(_stream(codec, 6), 1)
    assert result == AudioTrack(track=1, codec="copy")


def test_copy_friendly_returns_correct_track_num():
    result = prompt_audio_track_decision(_stream("ac3", 2), 3)
    assert result.track == 3
    assert result.codec == "copy"


# ---------------------------------------------------------------------------
# prompt_audio_track_decision — non-friendly codecs prompt
# ---------------------------------------------------------------------------

def test_truehd_51_default_y_produces_ac3_640():
    with patch("builtins.input", return_value=""):
        result = prompt_audio_track_decision(_stream("truehd", 6), 1)
    assert result == AudioTrack(track=1, codec="ac3", bitrate=640)


def test_truehd_20_default_y_produces_ac3_192():
    with patch("builtins.input", return_value=""):
        result = prompt_audio_track_decision(_stream("truehd", 2), 1)
    assert result == AudioTrack(track=1, codec="ac3", bitrate=192)


def test_truehd_mono_default_y_produces_ac3_96():
    with patch("builtins.input", return_value=""):
        result = prompt_audio_track_decision(_stream("truehd", 1), 1)
    assert result == AudioTrack(track=1, codec="ac3", bitrate=96)


def test_truehd_71_default_y_produces_ac3_640():
    with patch("builtins.input", return_value=""):
        result = prompt_audio_track_decision(_stream("truehd", 8), 1)
    assert result == AudioTrack(track=1, codec="ac3", bitrate=640)


def test_dts_prompt_fires_default_y_produces_ac3():
    with patch("builtins.input", return_value=""):
        result = prompt_audio_track_decision(_stream("dts", 6), 2)
    assert result == AudioTrack(track=2, codec="ac3", bitrate=640)


def test_non_friendly_explicit_y():
    with patch("builtins.input", return_value="y"):
        result = prompt_audio_track_decision(_stream("dts", 6), 1)
    assert result.codec == "ac3"
    assert result.bitrate == 640


def test_non_friendly_n_returns_copy():
    with patch("builtins.input", return_value="n"):
        result = prompt_audio_track_decision(_stream("dts", 6), 1)
    assert result == AudioTrack(track=1, codec="copy")


def test_non_friendly_c_custom_codec_bitrate():
    with patch("builtins.input", side_effect=["c", "eac3", "448"]):
        result = prompt_audio_track_decision(_stream("truehd", 6), 1)
    assert result == AudioTrack(track=1, codec="eac3", bitrate=448)


def test_non_friendly_unknown_channels_falls_back_to_640():
    # channels=None → default 2 → 192k (AC3_BITRATE_BY_CHANNELS[2])
    with patch("builtins.input", return_value=""):
        result = prompt_audio_track_decision(_stream("truehd", None), 1)
    assert result.codec == "ac3"
    assert result.bitrate == 192


# ---------------------------------------------------------------------------
# _audio_summary
# ---------------------------------------------------------------------------

def test_audio_summary_all_copy():
    tracks = [AudioTrack(track=1, codec="copy"), AudioTrack(track=2, codec="copy")]
    assert _audio_summary(tracks) == "[1:copy,2:copy]"


def test_audio_summary_all_transcode():
    tracks = [AudioTrack(track=1, codec="ac3", bitrate=640)]
    assert _audio_summary(tracks) == "[1:ac3@640]"


def test_audio_summary_mixed():
    tracks = [
        AudioTrack(track=1, codec="ac3", bitrate=640),
        AudioTrack(track=2, codec="copy"),
    ]
    assert _audio_summary(tracks) == "[1:ac3@640,2:copy]"


def test_audio_summary_empty():
    assert _audio_summary([]) == "[]"


# ---------------------------------------------------------------------------
# build_preview
# ---------------------------------------------------------------------------

def test_build_preview_copy_track():
    jobs = [_job("Movie", "/staging/Movie/Movie.mkv", [AudioTrack(track=1, codec="copy")])]
    text = build_preview(jobs)
    assert "Movie.mkv" in text
    assert "1:copy" in text


def test_build_preview_transcode_track():
    jobs = [_job("Movie", "/staging/Movie/Movie.mkv", [AudioTrack(track=1, codec="ac3", bitrate=640)])]
    text = build_preview(jobs)
    assert "1:ac3@640" in text


def test_build_preview_mixed_tracks_format():
    jobs = [_job(
        "Movie",
        "/staging/Movie/Movie.mkv",
        [AudioTrack(track=1, codec="ac3", bitrate=640), AudioTrack(track=2, codec="copy")],
    )]
    text = build_preview(jobs)
    assert "[1:ac3@640,2:copy]" in text


def test_build_preview_multiple_episodes():
    jobs = [
        _job(
            "S01E05",
            "/staging/Show/Season 01/Show - S01E05.mkv",
            [AudioTrack(track=1, codec="ac3", bitrate=640)],
        ),
        _job(
            "S01E06",
            "/staging/Show/Season 01/Show - S01E06.mkv",
            [AudioTrack(track=1, codec="ac3", bitrate=640)],
        ),
    ]
    text = build_preview(jobs)
    assert "S01E05" in text
    assert "S01E06" in text
    assert text.count("1:ac3@640") == 2


def test_build_preview_shows_crf_and_preset():
    jobs = [_job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")], quality=22, preset="medium")]
    text = build_preview(jobs)
    assert "crf=22" in text
    assert "preset=medium" in text


def test_build_preview_shows_subtitle_tracks():
    jobs = [_job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")], subtitle_tracks=[2])]
    text = build_preview(jobs)
    assert "s=[2]" in text


def test_build_preview_starts_with_preview_header():
    jobs = [_job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")])]
    text = build_preview(jobs)
    assert text.startswith("Preview:")


# ---------------------------------------------------------------------------
# argparse dispatcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _build_jobs_plan — raw source path layout
# ---------------------------------------------------------------------------

def _default_hb_args() -> HandbrakeArgs:
    return HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")], subtitle_tracks=[])


def test_build_jobs_plan_tv_source_path_uses_raw_dir():
    staging = pathlib.Path("/mnt/media/staging/Initial D")
    raw = staging / "_raw" / "batch-7"
    plan = _build_jobs_plan(
        is_tv=True,
        show="Initial D",
        season=1,
        episodes=[5],
        movie=None,
        staging_dir=staging,
        raw_dir=raw,
        surviving_indexes=[2],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(raw / "title_t02.mkv")


def test_build_jobs_plan_movie_source_path_uses_raw_dir():
    staging = pathlib.Path("/mnt/media/staging/Aliens")
    raw = staging / "_raw" / "batch-3"
    plan = _build_jobs_plan(
        is_tv=False,
        show=None,
        season=None,
        episodes=None,
        movie="Aliens",
        staging_dir=staging,
        raw_dir=raw,
        surviving_indexes=[0],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(raw / "title_t00.mkv")


def test_build_jobs_plan_output_path_unchanged_for_tv():
    staging = pathlib.Path("/mnt/media/staging/Initial D")
    raw = staging / "_raw" / "batch-7"
    plan = _build_jobs_plan(
        is_tv=True,
        show="Initial D",
        season=1,
        episodes=[5],
        movie=None,
        staging_dir=staging,
        raw_dir=raw,
        surviving_indexes=[2],
        hb_args=_default_hb_args(),
    )
    expected_out = str(staging / "Season 01" / "Initial D - S01E05.mkv")
    assert plan[0]["output_path"] == expected_out


def test_two_batches_same_show_non_overlapping_source_paths():
    staging = pathlib.Path("/mnt/media/staging/Initial D")
    raw_batch_1 = staging / "_raw" / "batch-1"
    raw_batch_2 = staging / "_raw" / "batch-2"
    hb = _default_hb_args()

    plan1 = _build_jobs_plan(
        is_tv=True, show="Initial D", season=1, episodes=[5, 6],
        movie=None, staging_dir=staging, raw_dir=raw_batch_1,
        surviving_indexes=[2, 3], hb_args=hb,
    )
    plan2 = _build_jobs_plan(
        is_tv=True, show="Initial D", season=1, episodes=[7, 8],
        movie=None, staging_dir=staging, raw_dir=raw_batch_2,
        surviving_indexes=[2, 3], hb_args=hb,
    )

    sources1 = {j["source_path"] for j in plan1}
    sources2 = {j["source_path"] for j in plan2}
    assert sources1.isdisjoint(sources2), "batch source paths must not overlap"


# ---------------------------------------------------------------------------
# argparse dispatcher
# ---------------------------------------------------------------------------

from rne.cli import _build_parser  # noqa: E402


def test_parser_ingest():
    args = _build_parser().parse_args(["ingest"])
    assert args.command == "ingest"


def test_parser_ls_defaults():
    args = _build_parser().parse_args(["ls"])
    assert args.command == "ls"
    assert not args.all
    assert args.status is None


def test_parser_ls_all():
    args = _build_parser().parse_args(["ls", "--all"])
    assert args.all


def test_parser_ls_status_filter():
    args = _build_parser().parse_args(["ls", "--status", "failed,done"])
    assert args.status == "failed,done"


def test_parser_edit():
    args = _build_parser().parse_args(["edit", "42"])
    assert args.command == "edit"
    assert args.id == 42


def test_parser_cancel():
    args = _build_parser().parse_args(["cancel", "7"])
    assert args.command == "cancel"
    assert args.id == 7


def test_parser_retry():
    args = _build_parser().parse_args(["retry", "3"])
    assert args.command == "retry"
    assert args.id == 3


def test_parser_pause():
    args = _build_parser().parse_args(["pause"])
    assert args.command == "pause"


def test_parser_resume():
    args = _build_parser().parse_args(["resume"])
    assert args.command == "resume"


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        _build_parser().parse_args([])
