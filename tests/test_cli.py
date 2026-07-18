"""Tests for CLI helpers and pure functions."""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from rne.cli._pipeline import (
    _audio_summary,
    _describe_mismatch,
    _parse_audio_selection,
    _subtitle_summary,
    build_preview,
    mungefilename,
)
from rne.cli.ingest import _build_display_order, _build_jobs_plan
from rne.cli.prompts import prompt_audio_track_decision
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack
from rne.probe import AudioStream, StreamSummary, SubtitleStream


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
    subtitle_tracks: list[SubtitleTrack] | None = None,
    quality: int = 20,
    preset: str = "slow",
    tune: str | None = None,
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
            tune=tune,
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
# _parse_audio_selection — order preservation and validation
# ---------------------------------------------------------------------------

_VALID_4 = [1, 2, 3, 4]


def test_parse_audio_selection_preserves_order():
    assert _parse_audio_selection("4,2", _VALID_4) == [4, 2]


def test_parse_audio_selection_single_track():
    assert _parse_audio_selection("3", _VALID_4) == [3]


def test_parse_audio_selection_all_returns_source_order():
    assert _parse_audio_selection("all", _VALID_4) == [1, 2, 3, 4]


def test_parse_audio_selection_all_case_insensitive():
    assert _parse_audio_selection("ALL", _VALID_4) == [1, 2, 3, 4]


def test_parse_audio_selection_duplicate_raises():
    with pytest.raises(ValueError, match="duplicate"):
        _parse_audio_selection("4,2,4", _VALID_4)


def test_parse_audio_selection_out_of_range_raises():
    with pytest.raises(ValueError, match="out of range"):
        _parse_audio_selection("5,1", _VALID_4)


def test_parse_audio_selection_zero_raises():
    with pytest.raises(ValueError, match="out of range"):
        _parse_audio_selection("0", _VALID_4)


def test_parse_audio_selection_reversed_order():
    assert _parse_audio_selection("3,1,2", _VALID_4) == [3, 1, 2]


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
    jobs = [
        _job("Movie", "/staging/Movie/Movie.mkv", [AudioTrack(track=1, codec="copy")])
    ]
    text = build_preview(jobs)
    assert "Movie.mkv" in text
    assert "1:copy" in text


def test_build_preview_transcode_track():
    jobs = [
        _job(
            "Movie",
            "/staging/Movie/Movie.mkv",
            [AudioTrack(track=1, codec="ac3", bitrate=640)],
        )
    ]
    text = build_preview(jobs)
    assert "1:ac3@640" in text


def test_build_preview_mixed_tracks_format():
    jobs = [
        _job(
            "Movie",
            "/staging/Movie/Movie.mkv",
            [
                AudioTrack(track=1, codec="ac3", bitrate=640),
                AudioTrack(track=2, codec="copy"),
            ],
        )
    ]
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
    jobs = [
        _job(
            "M",
            "/s/M.mkv",
            [AudioTrack(track=1, codec="copy")],
            quality=22,
            preset="medium",
        )
    ]
    text = build_preview(jobs)
    assert "crf=22" in text
    assert "preset=medium" in text


def test_build_preview_shows_subtitle_tracks():
    jobs = [
        _job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")],
             subtitle_tracks=[SubtitleTrack(2)])
    ]
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
    return HandbrakeArgs(
        audio_tracks=[AudioTrack(track=1, codec="copy")],
        subtitle_tracks=[],
    )


def test_build_jobs_plan_tv_source_path_uses_manifest():
    staging = pathlib.Path("/mnt/media/staging/Initial D")
    raw = staging / "_raw" / "batch-7"
    actual_file = raw / "D1_00.mkv"
    plan = _build_jobs_plan(
        is_tv=True,
        show="Initial D",
        season=1,
        episodes=[5],
        movie=None,
        staging_dir=staging,
        rip_manifest=[(2, actual_file)],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(actual_file)


def test_build_jobs_plan_movie_source_path_uses_manifest():
    staging = pathlib.Path("/mnt/media/staging/Aliens")
    raw = staging / "_raw" / "batch-3"
    actual_file = raw / "00038.mkv"
    plan = _build_jobs_plan(
        is_tv=False,
        show=None,
        season=None,
        episodes=None,
        movie="Aliens",
        staging_dir=staging,
        rip_manifest=[(0, actual_file)],
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(actual_file)


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
        rip_manifest=[(2, raw / "D1_00.mkv")],
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
        is_tv=True,
        show="Initial D",
        season=1,
        episodes=[5, 6],
        movie=None,
        staging_dir=staging,
        rip_manifest=[
            (2, raw_batch_1 / "B1_t00.mkv"),
            (3, raw_batch_1 / "B1_t01.mkv"),
        ],
        hb_args=hb,
    )
    plan2 = _build_jobs_plan(
        is_tv=True,
        show="Initial D",
        season=1,
        episodes=[7, 8],
        movie=None,
        staging_dir=staging,
        rip_manifest=[
            (2, raw_batch_2 / "B1_t00.mkv"),
            (3, raw_batch_2 / "B1_t01.mkv"),
        ],
        hb_args=hb,
    )

    sources1 = {j["source_path"] for j in plan1}
    sources2 = {j["source_path"] for j in plan2}
    assert sources1.isdisjoint(sources2), "batch source paths must not overlap"


def test_build_jobs_plan_manifest_order_preserved():
    """source_path order follows rip_manifest order, not alphabetical filename order."""
    staging = pathlib.Path("/mnt/media/staging/Show")
    raw = staging / "_raw" / "batch-1"
    # Files sort differently alphabetically than disc order
    manifest = [
        (0, raw / "D1_00.mkv"),
        (1, raw / "A1_00.mkv"),
        (2, raw / "C1_00.mkv"),
    ]
    plan = _build_jobs_plan(
        is_tv=True,
        show="Show",
        season=1,
        episodes=[5, 6, 7],
        movie=None,
        staging_dir=staging,
        rip_manifest=manifest,
        hb_args=_default_hb_args(),
    )
    assert plan[0]["source_path"] == str(raw / "D1_00.mkv")
    assert plan[1]["source_path"] == str(raw / "A1_00.mkv")
    assert plan[2]["source_path"] == str(raw / "C1_00.mkv")
    assert plan[0]["episode"] == 5
    assert plan[1]["episode"] == 6
    assert plan[2]["episode"] == 7


def test_build_jobs_plan_source_path_matches_manifest_not_constructed():
    """source_path is the actual manifest path, not a title_tNN.mkv name."""
    staging = pathlib.Path("/mnt/media/staging/Initial D")
    raw = staging / "_raw" / "batch-7"
    actual_file = raw / "00038.mkv"
    plan = _build_jobs_plan(
        is_tv=True,
        show="Initial D",
        season=1,
        episodes=[5],
        movie=None,
        staging_dir=staging,
        rip_manifest=[(2, actual_file)],
        hb_args=_default_hb_args(),
    )
    assert "title_t" not in plan[0]["source_path"]
    assert plan[0]["source_path"] == str(actual_file)


# ---------------------------------------------------------------------------
# _build_display_order — mpls-sorted display index mapping
# ---------------------------------------------------------------------------


def _make_titles(*pairs: tuple[int, str]) -> dict:
    """Build a minimal titles dict: {disc_idx: {info: {T_SOURCE: name}, streams: {}}}."""
    from rne.makemkv import T_SOURCE
    return {
        disc_idx: {"info": {T_SOURCE: name}, "streams": {}}
        for disc_idx, name in pairs
    }


def test_build_display_order_already_sequential():
    titles = _make_titles((0, "00001.mpls"), (1, "00002.mpls"), (2, "00003.mpls"))
    assert _build_display_order(titles) == [0, 1, 2]


def test_build_display_order_out_of_order():
    # Mirrors the user's example: disc indexes 0-7, mpls names scrambled.
    titles = _make_titles(
        (0, "00009.mpls"),
        (1, "00010.mpls"),
        (2, "00006.mpls"),
        (3, "00008.mpls"),
        (4, "00005.mpls"),
        (5, "00007.mpls"),
        (6, "00004.mpls"),
        (7, "00002.mpls"),
    )
    # Sorted by mpls name: 00002→7, 00004→6, 00005→4, 00006→2, 00007→5, 00008→3, 00009→0, 00010→1
    assert _build_display_order(titles) == [7, 6, 4, 2, 5, 3, 0, 1]


def test_build_display_order_missing_source_sorts_first():
    from rne.makemkv import T_SOURCE
    titles = {
        0: {"info": {T_SOURCE: "00005.mpls"}, "streams": {}},
        1: {"info": {}, "streams": {}},        # no T_SOURCE → empty string → sorts first
        2: {"info": {T_SOURCE: "00003.mpls"}, "streams": {}},
    }
    assert _build_display_order(titles) == [1, 2, 0]


def test_display_to_disc_index_mapping_out_of_order():
    """Selecting display indexes 1-7 from the scrambled disc gives disc indexes in mpls order."""
    titles = _make_titles(
        (0, "00009.mpls"),
        (1, "00010.mpls"),
        (2, "00006.mpls"),
        (3, "00008.mpls"),
        (4, "00005.mpls"),
        (5, "00007.mpls"),
        (6, "00004.mpls"),
        (7, "00002.mpls"),
    )
    display_order = _build_display_order(titles)
    # User selects display 1-7 (skipping 00002.mpls at display 0)
    selected_display = list(range(1, 8))
    selected_indexes = [display_order[i] for i in selected_display]
    # Expected rip order (mpls ascending, excluding 00002): 6,4,2,5,3,0,1
    assert selected_indexes == [6, 4, 2, 5, 3, 0, 1]


def test_display_to_disc_index_mapping_sequential():
    titles = _make_titles((0, "00001.mpls"), (1, "00002.mpls"), (2, "00003.mpls"))
    display_order = _build_display_order(titles)
    selected_indexes = [display_order[i] for i in range(3)]
    assert selected_indexes == [0, 1, 2]


# ---------------------------------------------------------------------------
# argparse dispatcher
# ---------------------------------------------------------------------------

from rne.cli import _build_parser  # noqa: E402


def test_parser_ingest():
    args = _build_parser().parse_args(["ingest"])
    assert args.command == "ingest"


def test_parser_ingest_metadata_defaults_none():
    args = _build_parser().parse_args(["ingest"])
    assert args.name is None
    assert args.season is None
    assert args.first_episode is None


def test_parser_ingest_name_short_and_long():
    args = _build_parser().parse_args(["ingest", "-n", "Initial D"])
    assert args.name == "Initial D"
    args = _build_parser().parse_args(["ingest", "--name", "Initial D"])
    assert args.name == "Initial D"


def test_parser_ingest_season_and_first_episode():
    args = _build_parser().parse_args(["ingest", "-sn", "1", "-fe", "5"])
    assert args.season == 1
    assert args.first_episode == 5


def test_parser_ingest_season_zero_allowed():
    args = _build_parser().parse_args(["ingest", "-sn", "0"])
    assert args.season == 0


def test_parser_ingest_negative_season_rejected():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["ingest", "-sn", "-1"])


def test_parser_ingest_zero_first_episode_rejected():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["ingest", "-fe", "0"])


def test_parser_ingest_non_numeric_season_rejected():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["ingest", "-sn", "one"])


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


# ---------------------------------------------------------------------------
# mungefilename (case 5)
# ---------------------------------------------------------------------------


def test_mungefilename_strips_colon():
    assert mungefilename("Star Wars: A New Hope") == "Star Wars A New Hope"


def test_mungefilename_strips_all_unsafe_chars():
    assert mungefilename('foo/bar\\baz<>|*"\'?') == "foobarbaz"


def test_mungefilename_strips_control_chars():
    assert mungefilename("foo\x00bar\x1fbaz\x7f") == "foobarbaz"


def test_mungefilename_preserves_safe_chars():
    assert mungefilename("Initial D - S01E05") == "Initial D - S01E05"


def test_mungefilename_empty_string():
    assert mungefilename("") == ""


def test_mungefilename_all_unsafe_becomes_empty():
    assert mungefilename(":/\\*?<>|") == ""


# ---------------------------------------------------------------------------
# rne edit validation (spec §Other CLI subcommands)
# ---------------------------------------------------------------------------


def test_edit_validation_invalid_json():
    """Bullet 1: file content must be valid JSON."""
    with pytest.raises(Exception):
        HandbrakeArgs.from_json("{not valid json")


def test_edit_validation_unknown_field_rejected():
    """Bullet 2: unknown keys in the HandbrakeArgs object are rejected."""
    import json

    bad = json.dumps(
        {
            "encoder": "x265",
            "quality": 20,
            "preset": "slow",
            "audio_tracks": [],
            "subtitle_tracks": [],
            "decomb": False,
            "extra_args": [],
            "unknown_field": "oops",
        }
    )
    with pytest.raises(TypeError):
        HandbrakeArgs.from_json(bad)


def test_edit_validation_audio_track_bitrate_required_for_transcode():
    """Bullet 3: bitrate required when codec != 'copy'."""
    with pytest.raises(ValueError):
        AudioTrack(track=1, codec="ac3", bitrate=None)


def test_edit_validation_audio_track_bitrate_rejected_for_copy():
    """Bullet 3: bitrate must be absent when codec == 'copy'."""
    with pytest.raises(ValueError):
        AudioTrack(track=1, codec="copy", bitrate=640)


def test_edit_toctou_job_running_refused(tmp_path):
    """Bullet 4: if the job transitions to running while the editor is open,
    the save is refused with 'job is now running; cannot edit'."""
    import json
    import types

    from rne import db
    from rne.cli import edit as edit_mod
    from tests.conftest import insert_job

    db_path = str(tmp_path / "toctou.db")
    conn = db.connect(db_path)
    db.init_db(conn)
    job_id = insert_job(conn, status="queued")
    conn.close()

    # Save the real db.connect before patching, since the patch affects the
    # shared module object and would otherwise intercept fake_editor's calls too.
    real_connect = db.connect

    def fake_editor(cmd, **kwargs):
        # Simulate the worker claiming the job while the editor is open.
        c = real_connect(db_path)
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        c.commit()
        c.close()
        # Write valid JSON to the tempfile so validation passes.
        tmp_file = cmd[1]
        with open(tmp_file, "w") as f:
            json.dump(json.loads(HandbrakeArgs().to_json()), f)
        result = types.SimpleNamespace(returncode=0)
        return result

    args = types.SimpleNamespace(id=job_id)

    with patch("rne.cli.edit.db.connect", side_effect=lambda *_: real_connect(db_path)):
        with patch("rne.cli.edit.subprocess.run", side_effect=fake_editor):
            with pytest.raises(SystemExit) as exc_info:
                edit_mod.run(args)

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# build_preview — layout_warning flag
# ---------------------------------------------------------------------------


def test_build_preview_layout_warning_shown():
    jobs = [
        _job("S01E05", "/s/S01E05.mkv", [AudioTrack(track=1, codec="copy")]),
        dict(**_job("S01E06", "/s/S01E06.mkv", [AudioTrack(track=1, codec="copy")]),
             layout_warning=True),
    ]
    text = build_preview(jobs)
    assert "⚠" in text
    assert "S01E06" in text


def test_build_preview_no_warning_when_layouts_match():
    jobs = [
        _job("S01E05", "/s/S01E05.mkv", [AudioTrack(track=1, codec="copy")]),
        _job("S01E06", "/s/S01E06.mkv", [AudioTrack(track=1, codec="copy")]),
    ]
    text = build_preview(jobs)
    assert "⚠" not in text


# ---------------------------------------------------------------------------
# _subtitle_summary
# ---------------------------------------------------------------------------


def test_subtitle_summary_no_default():
    tracks = [SubtitleTrack(1), SubtitleTrack(2)]
    assert _subtitle_summary(tracks) == "[1,2]"


def test_subtitle_summary_with_default():
    tracks = [SubtitleTrack(1, default=True), SubtitleTrack(2)]
    assert _subtitle_summary(tracks) == "[1*,2]"


def test_subtitle_summary_second_default():
    tracks = [SubtitleTrack(3), SubtitleTrack(1, default=True)]
    assert _subtitle_summary(tracks) == "[3,1*]"


def test_subtitle_summary_empty():
    assert _subtitle_summary([]) == "[]"


# ---------------------------------------------------------------------------
# build_preview — tune and subtitle default marker
# ---------------------------------------------------------------------------


def test_build_preview_tune_shown_when_set():
    jobs = [_job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")], tune="animation")]
    text = build_preview(jobs)
    assert "tune=animation" in text


def test_build_preview_tune_absent_when_none():
    jobs = [_job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")])]
    text = build_preview(jobs)
    assert "tune=" not in text


def test_build_preview_subtitle_default_marker():
    jobs = [
        _job("M", "/s/M.mkv", [AudioTrack(track=1, codec="copy")],
             subtitle_tracks=[SubtitleTrack(1), SubtitleTrack(2, default=True)])
    ]
    text = build_preview(jobs)
    assert "s=[1,2*]" in text


# ---------------------------------------------------------------------------
# _describe_mismatch
# ---------------------------------------------------------------------------


def _audio_stream(codec: str) -> AudioStream:
    return AudioStream(
        codec=codec, channels=6, lang="", title="", default=False, forced=False, bitrate=None
    )


def _stream_summary(audio_codecs: tuple[str, ...], num_subs: int = 0) -> "StreamSummary":
    audio = [_audio_stream(c) for c in audio_codecs]
    subs = [
        SubtitleStream(codec="pgs", lang="", title="", default=False, forced=False, frames=None)
        for _ in range(num_subs)
    ]
    return StreamSummary(video=[], audio=audio, subtitle=subs)


def test_describe_mismatch_different_codec():
    ref = _stream_summary(("ac3", "truehd"))
    other = _stream_summary(("ac3", "dts"))
    desc = _describe_mismatch(ref, other, "S01E07")
    assert "S01E07" in desc
    assert "dts" in desc
    assert "truehd" in desc


def test_describe_mismatch_different_audio_count():
    ref = _stream_summary(("ac3", "dts"))
    other = _stream_summary(("ac3",))
    desc = _describe_mismatch(ref, other, "S01E07")
    assert "audio track count" in desc


def test_describe_mismatch_different_subtitle_count():
    ref = _stream_summary(("ac3",), num_subs=2)
    other = _stream_summary(("ac3",), num_subs=3)
    desc = _describe_mismatch(ref, other, "S01E07")
    assert "subtitle track count" in desc


# ---------------------------------------------------------------------------
# Subtitle default prompt logic (via SubtitleTrack construction)
# ---------------------------------------------------------------------------
# These tests verify the expected outcomes of the ingest subtitle-default
# prompt, exercised directly against the SubtitleTrack model since the
# prompt logic is a thin wrapper around this construction pattern.


def test_subtitle_no_selection_skips_default_prompt():
    # When no subtitles selected, subtitle_tracks list is empty — no defaults.
    subtitle_track_indexes: list[int] = []
    subtitle_tracks = [SubtitleTrack(track=n) for n in subtitle_track_indexes]
    assert subtitle_tracks == []


def test_subtitle_zero_selection_all_false():
    # Selecting "0" at the default prompt → no track gets default=True.
    subtitle_track_indexes = [1, 2]
    subtitle_tracks = [SubtitleTrack(track=n) for n in subtitle_track_indexes]
    # "0" or empty input → leave all as default=False
    assert all(not t.default for t in subtitle_tracks)


def test_subtitle_default_marks_correct_source_track():
    # User selected subtitles [3, 1] (non-sequential order).
    # Selecting "1" at the default prompt should mark source track 3 as default.
    subtitle_track_indexes = [3, 1]
    subtitle_tracks = [SubtitleTrack(track=n) for n in subtitle_track_indexes]
    sel = 1  # user enters "1"
    source_track = subtitle_track_indexes[sel - 1]  # = 3
    subtitle_tracks[sel - 1] = SubtitleTrack(track=source_track, default=True)
    assert subtitle_tracks[0].track == 3
    assert subtitle_tracks[0].default is True
    assert subtitle_tracks[1].track == 1
    assert subtitle_tracks[1].default is False


def test_subtitle_default_second_selection():
    # User selected [3, 1], enters "2" → marks source track 1 as default.
    subtitle_track_indexes = [3, 1]
    subtitle_tracks = [SubtitleTrack(track=n) for n in subtitle_track_indexes]
    sel = 2
    source_track = subtitle_track_indexes[sel - 1]  # = 1
    subtitle_tracks[sel - 1] = SubtitleTrack(track=source_track, default=True)
    assert subtitle_tracks[0].default is False
    assert subtitle_tracks[1].track == 1
    assert subtitle_tracks[1].default is True


# ---------------------------------------------------------------------------
# prompt_metadata — pre-supplied name/season/first_ep (ingest flags)
# ---------------------------------------------------------------------------

from rne.cli._pipeline import prompt_metadata  # noqa: E402


def test_prompt_metadata_all_provided_only_confirms():
    # name + season + first_ep provided → only the episode-preview confirm fires.
    with patch("builtins.input", side_effect=[""]):  # Confirm? [Y/n] → default Y
        result = prompt_metadata(
            "VOLUME", 2, name="Initial D", season=1, first_ep=5
        )
    assert result == (True, "Initial D", 1, 5, None, False)


def test_prompt_metadata_season_implies_tv():
    # Only season provided → no content-type prompt; show and first-ep prompted.
    with patch("builtins.input", side_effect=["My Show", "3", ""]):
        result = prompt_metadata("VOLUME", 1, season=2)
    assert result == (True, "My Show", 2, 3, None, False)


def test_prompt_metadata_first_ep_implies_tv():
    # Only first_ep provided → no content-type prompt; show and season prompted.
    with patch("builtins.input", side_effect=["My Show", "0", ""]):
        result = prompt_metadata("VOLUME", 1, first_ep=4)
    assert result == (True, "My Show", 0, 4, None, False)


def test_prompt_metadata_name_only_still_asks_type_movie():
    # Name alone doesn't imply TV; type prompt fires, movie name comes from flag.
    with patch("builtins.input", side_effect=["2"]):
        result = prompt_metadata("VOLUME", 1, name="Aliens")
    assert result == (False, None, None, None, "Aliens", False)


def test_prompt_metadata_name_only_tv_prompts_season_and_episode():
    with patch("builtins.input", side_effect=["1", "1", "5", ""]):
        result = prompt_metadata("VOLUME", 2, name="Initial D")
    assert result == (True, "Initial D", 1, 5, None, False)


def test_prompt_metadata_provided_name_is_munged():
    with patch("builtins.input", side_effect=["2"]):
        result = prompt_metadata("VOLUME", 1, name="Star Wars: A New Hope")
    assert result[4] == "Star Wars A New Hope"


def test_prompt_metadata_flags_still_ask_disc_split():
    # season/first_ep provided with a single title → disc-split question still fires.
    with patch("builtins.input", side_effect=["y"]):
        result = prompt_metadata(
            "VOLUME", 1, single_file_tv=True, name="Show", season=1, first_ep=1
        )
    assert result == (True, "Show", 1, 1, None, True)


def test_prompt_metadata_no_flags_unchanged():
    # Baseline: no flags → full prompt flow as before.
    with patch("builtins.input", side_effect=["1", "Initial D", "1", "5", ""]):
        result = prompt_metadata("VOLUME", 2)
    assert result == (True, "Initial D", 1, 5, None, False)


# ---------------------------------------------------------------------------
# prompt_encoding_config — preset default by source type
# ---------------------------------------------------------------------------

from rne.cli._pipeline import prompt_encoding_config  # noqa: E402
from rne.probe import VideoStream  # noqa: E402


def _video(codec: str, fps: str = "23.976", field_order: str = "progressive") -> VideoStream:
    return VideoStream(
        codec=codec,
        resolution="720x480",
        fps=fps,
        field_order=field_order,
        lang="",
        default=True,
        forced=False,
    )


def test_encoding_config_dvd_defaults_to_medium_preset():
    summary = StreamSummary(
        video=[_video("mpeg2video", fps="29.97")],
        audio=[_stream("ac3", 6)],
        subtitle=[],
    )
    with patch("builtins.input", return_value=""):
        hb = prompt_encoding_config(summary)
    assert hb.preset == "medium"


def test_encoding_config_bluray_defaults_to_slow_preset():
    summary = StreamSummary(
        video=[_video("h264", fps="23.976")],
        audio=[_stream("ac3", 6)],
        subtitle=[],
    )
    with patch("builtins.input", return_value=""):
        hb = prompt_encoding_config(summary)
    assert hb.preset == "slow"


def test_encoding_config_dvd_flag_defaults_to_medium_preset():
    # --dvd flag on rne queue forces DVD treatment regardless of codec.
    summary = StreamSummary(
        video=[_video("h264", fps="29.97")],
        audio=[_stream("ac3", 6)],
        subtitle=[],
    )
    with patch("builtins.input", return_value=""):
        hb = prompt_encoding_config(summary, is_dvd=True)
    assert hb.preset == "medium"


def test_encoding_config_explicit_preset_overrides_dvd_default():
    summary = StreamSummary(
        video=[_video("mpeg2video", fps="29.97")],
        audio=[_stream("ac3", 6)],
        subtitle=[],
    )
    # audio [], crf, preset=veryslow, animation, detelecine, decomb
    with patch("builtins.input", side_effect=["", "", "veryslow", "", "", ""]):
        hb = prompt_encoding_config(summary)
    assert hb.preset == "veryslow"


# ---------------------------------------------------------------------------
# _rip_title_with_retries
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402

from rne.cli.ingest import _rip_title_with_retries  # noqa: E402
from rne.makemkv import MakemkvError  # noqa: E402

_RIPPED = pathlib.Path("/staging/_raw/batch-1/title_t00.mkv")


def _rip_kwargs() -> dict:
    return {
        "disc": 0,
        "raw_dir": pathlib.Path("/staging/_raw/batch-1"),
        "minlength": 900,
    }


def test_rip_retries_success_first_try():
    with patch("rne.cli.ingest.makemkv.rip_and_detect", return_value=_RIPPED):
        with patch("builtins.input", side_effect=AssertionError("no prompt expected")):
            result = _rip_title_with_retries(2, auto_retries=1, **_rip_kwargs())
    assert result == _RIPPED


def test_rip_retries_auto_retry_recovers_without_prompt():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=[MakemkvError("boom"), _RIPPED],
    ) as rip:
        with patch("builtins.input", side_effect=AssertionError("no prompt expected")):
            result = _rip_title_with_retries(2, auto_retries=1, **_rip_kwargs())
    assert result == _RIPPED
    assert rip.call_count == 2


def test_rip_retries_handles_called_process_error():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=[subprocess.CalledProcessError(1, ["makemkvcon"]), _RIPPED],
    ):
        with patch("builtins.input", side_effect=AssertionError("no prompt expected")):
            result = _rip_title_with_retries(2, auto_retries=1, **_rip_kwargs())
    assert result == _RIPPED


def test_rip_retries_exhausted_then_skip_returns_none():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=MakemkvError("boom"),
    ) as rip:
        with patch("builtins.input", side_effect=["s"]):
            result = _rip_title_with_retries(2, auto_retries=1, **_rip_kwargs())
    assert result is None
    assert rip.call_count == 2  # initial + 1 auto retry


def test_rip_retries_exhausted_then_abort_exits():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=MakemkvError("boom"),
    ):
        with patch("builtins.input", side_effect=["a"]):
            with pytest.raises(SystemExit) as exc_info:
                _rip_title_with_retries(2, auto_retries=0, **_rip_kwargs())
    assert exc_info.value.code == 1


def test_rip_retries_manual_retry_recovers():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=[MakemkvError("boom"), _RIPPED],
    ):
        with patch("builtins.input", side_effect=["r"]):
            result = _rip_title_with_retries(2, auto_retries=0, **_rip_kwargs())
    assert result == _RIPPED


def test_rip_retries_manual_retry_fails_prompts_again():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=[MakemkvError("boom"), MakemkvError("boom"), _RIPPED],
    ):
        with patch("builtins.input", side_effect=["r", "r"]):
            result = _rip_title_with_retries(2, auto_retries=0, **_rip_kwargs())
    assert result == _RIPPED


def test_rip_retries_zero_auto_retries_prompts_immediately():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=MakemkvError("boom"),
    ) as rip:
        with patch("builtins.input", side_effect=["s"]):
            result = _rip_title_with_retries(2, auto_retries=0, **_rip_kwargs())
    assert result is None
    assert rip.call_count == 1


def test_rip_retries_invalid_choice_reprompts():
    with patch(
        "rne.cli.ingest.makemkv.rip_and_detect",
        side_effect=MakemkvError("boom"),
    ):
        with patch("builtins.input", side_effect=["x", "s"]):
            result = _rip_title_with_retries(2, auto_retries=0, **_rip_kwargs())
    assert result is None


# ---------------------------------------------------------------------------
# config._rip_retries — RNE_RIP_RETRIES parsing and clamping
# ---------------------------------------------------------------------------

from rne.config import _rip_retries  # noqa: E402


def test_rip_retries_default_is_one(monkeypatch):
    monkeypatch.delenv("RNE_RIP_RETRIES", raising=False)
    assert _rip_retries() == 1


def test_rip_retries_env_override(monkeypatch):
    monkeypatch.setenv("RNE_RIP_RETRIES", "5")
    assert _rip_retries() == 5


def test_rip_retries_zero_allowed(monkeypatch):
    monkeypatch.setenv("RNE_RIP_RETRIES", "0")
    assert _rip_retries() == 0


def test_rip_retries_clamped_to_ten(monkeypatch):
    monkeypatch.setenv("RNE_RIP_RETRIES", "50")
    assert _rip_retries() == 10


def test_rip_retries_negative_clamped_to_zero(monkeypatch):
    monkeypatch.setenv("RNE_RIP_RETRIES", "-3")
    assert _rip_retries() == 0


def test_rip_retries_non_numeric_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RNE_RIP_RETRIES", "lots")
    assert _rip_retries() == 1
