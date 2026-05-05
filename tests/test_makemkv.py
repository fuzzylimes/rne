"""Tests for makemkv.py parser — feed real makemkvcon -r output from fixtures."""

from pathlib import Path

import pytest

from rne.makemkv import (
    C_VOLUME_NAME,
    T_CHAPTERS,
    T_DURATION,
    T_SIZE,
    T_SOURCE,
    parse_index_spec,
    parse_info,
    summarize,
)

FIXTURE = Path(__file__).parent / "fixtures" / "makemkv_info_sample.txt"


@pytest.fixture(scope="module")
def parsed():
    return parse_info(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# parse_info — disc_info
# ---------------------------------------------------------------------------

def test_disc_volume_name(parsed):
    disc_info, _ = parsed
    assert disc_info[C_VOLUME_NAME] == "Example Movie Title"


def test_disc_info_has_entries(parsed):
    disc_info, _ = parsed
    assert len(disc_info) > 0


# ---------------------------------------------------------------------------
# parse_info — titles
# ---------------------------------------------------------------------------

def test_title_count(parsed):
    _, titles = parsed
    assert len(titles) == 12


def test_all_title_ids_present(parsed):
    _, titles = parsed
    assert sorted(titles.keys()) == list(range(12))


def test_title_zero_has_info(parsed):
    _, titles = parsed
    info = titles[0]["info"]
    assert info[T_DURATION] == "1:30:32"
    assert info[T_SIZE] == "23.7 GB"
    assert info[T_CHAPTERS] == "16"
    assert info[T_SOURCE] == "00011.mpls"


def test_title_zero_has_streams(parsed):
    _, titles = parsed
    streams = titles[0]["streams"]
    assert len(streams) >= 2  # at least video + audio


def test_title_has_video_and_audio_streams(parsed):
    _, titles = parsed
    streams = titles[0]["streams"]
    types = {s.get(1) for s in streams.values()}
    assert "Video" in types
    assert "Audio" in types


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def title0_summary(parsed):
    _, titles = parsed
    return summarize(0, titles[0])


def test_summary_id(title0_summary):
    assert title0_summary["#"] == 0


def test_summary_source(title0_summary):
    assert title0_summary["Source"] == "00011.mpls"


def test_summary_duration(title0_summary):
    assert title0_summary["Duration"] == "1:30:32"


def test_summary_size(title0_summary):
    assert title0_summary["Size"] == "23.7 GB"


def test_summary_chapters(title0_summary):
    assert title0_summary["Ch"] == "16"


def test_summary_resolution(title0_summary):
    assert title0_summary["Resolution"] == "1920x1080"


def test_summary_fps_stripped(title0_summary):
    # Reference strips the "(24000/1001)" parenthetical
    fps = title0_summary["FPS"]
    assert fps == "23.976"
    assert "(" not in fps


def test_summary_audio_has_codec_and_lang(title0_summary):
    audio = title0_summary["Audio"]
    assert "TrueHD" in audio
    assert "jpn" in audio


def test_summary_audio_has_track_name(title0_summary):
    assert "Surround 5.1" in title0_summary["Audio"]


def test_summary_empty_title():
    empty = {"info": {}, "streams": {}}
    s = summarize(99, empty)
    assert s["#"] == 99
    assert s["Duration"] == ""
    assert s["Audio"] == ""
    assert s["Resolution"] == ""


# ---------------------------------------------------------------------------
# parse_index_spec
# ---------------------------------------------------------------------------

def test_all_returns_none():
    assert parse_index_spec("all") is None


def test_all_case_insensitive():
    assert parse_index_spec("ALL") is None
    assert parse_index_spec("All") is None


def test_single_index():
    assert parse_index_spec("5") == [5]


def test_comma_separated():
    assert parse_index_spec("0,2,4") == [0, 2, 4]


def test_range():
    assert parse_index_spec("0-3") == [0, 1, 2, 3]


def test_mixed_range_and_singles():
    assert parse_index_spec("0-3,5,7") == [0, 1, 2, 3, 5, 7]


def test_space_separated():
    assert parse_index_spec("0 1 2") == [0, 1, 2]


def test_output_is_sorted():
    assert parse_index_spec("7,0,3") == [0, 3, 7]


def test_deduplication():
    assert parse_index_spec("0-3,2,3") == [0, 1, 2, 3]


def test_empty_parts_ignored():
    # Extra commas should not cause an error
    assert parse_index_spec("0,,2") == [0, 2]
