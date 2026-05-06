"""Tests for probe.py summarize() — fed real ffprobe JSON from fixtures."""

import json
from pathlib import Path

import pytest

from rne.probe import StreamSummary, summarize

FIXTURE = Path(__file__).parent / "fixtures" / "ffprobe_sample.json"


@pytest.fixture(scope="module")
def probe_data():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def summary(probe_data) -> StreamSummary:
    return summarize(probe_data)


# ---------------------------------------------------------------------------
# Overall shape
# ---------------------------------------------------------------------------


def test_returns_stream_summary(summary):
    assert isinstance(summary, StreamSummary)


def test_video_count(summary):
    assert len(summary.video) == 1


def test_audio_count(summary):
    assert len(summary.audio) == 11


def test_subtitle_count(summary):
    assert len(summary.subtitle) == 14


# ---------------------------------------------------------------------------
# Video stream
# ---------------------------------------------------------------------------


def test_video_codec(summary):
    assert summary.video[0].codec == "mpeg2video"


def test_video_resolution(summary):
    assert summary.video[0].resolution == "1920x1080"


def test_video_fps(summary):
    # 24000/1001 ≈ 23.976
    fps = summary.video[0].fps
    assert fps.startswith("23.976")


def test_video_field_order(summary):
    assert summary.video[0].field_order == "progressive"


def test_video_lang(summary):
    assert summary.video[0].lang == "eng"


def test_video_default_false(summary):
    assert summary.video[0].default is False


def test_video_forced_false(summary):
    assert summary.video[0].forced is False


# ---------------------------------------------------------------------------
# Audio streams — ordering and basic fields
# ---------------------------------------------------------------------------


def test_audio_first_codec(summary):
    # First audio in the file is TrueHD (jpn, default track)
    assert summary.audio[0].codec == "truehd"


def test_audio_first_channels(summary):
    assert summary.audio[0].channels == 6


def test_audio_ac3_channels(summary):
    assert summary.audio[1].channels == 6


def test_audio_first_lang(summary):
    assert summary.audio[0].lang == "jpn"


def test_audio_first_default(summary):
    assert summary.audio[0].default is True


def test_audio_ac3_codec(summary):
    # Second audio is AC3
    assert summary.audio[1].codec == "ac3"


def test_audio_ac3_bitrate_direct(summary):
    # AC3 streams have stream-level bit_rate
    assert summary.audio[1].bitrate == 448000


def test_audio_truehd_bitrate_fallback(summary):
    # TrueHD has no stream bit_rate — falls back to format bit_rate // num_audio
    # format bit_rate=34038867, 11 audio streams → 34038867 // 11 = 3094442
    assert summary.audio[0].bitrate == 34038867 // 11


def test_audio_title_present(summary):
    assert summary.audio[0].title == "Surround 5.1"


def test_audio_languages_cover_expected(summary):
    langs = [a.lang for a in summary.audio]
    assert "jpn" in langs
    assert "eng" in langs


# ---------------------------------------------------------------------------
# Subtitle streams
# ---------------------------------------------------------------------------


def test_subtitle_first_codec(summary):
    assert summary.subtitle[0].codec == "hdmv_pgs_subtitle"


def test_subtitle_duration_present(summary):
    # All PGS subtitles in this fixture have a header duration
    for sub in summary.subtitle:
        assert sub.duration is not None
        assert sub.duration > 0


def test_subtitle_duration_value(summary):
    assert summary.subtitle[0].duration == pytest.approx(5432.448, rel=1e-4)


def test_subtitle_default_flag(summary):
    defaults = [s for s in summary.subtitle if s.default]
    assert len(defaults) == 1


def test_subtitle_forced_none(summary):
    # No forced subtitles in this fixture
    assert all(not s.forced for s in summary.subtitle)


def test_subtitle_languages(summary):
    langs = [s.lang for s in summary.subtitle]
    assert "eng" in langs
    assert "fra" in langs


# ---------------------------------------------------------------------------
# summarize with no-duration subtitles
# ---------------------------------------------------------------------------


def test_subtitle_duration_none_when_absent():
    data = {
        "streams": [
            {
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "disposition": {},
                "tags": {"language": "eng"},
                # no "duration" key
            }
        ],
        "format": {},
    }
    s = summarize(data)
    assert s.subtitle[0].duration is None


# ---------------------------------------------------------------------------
# summarize with empty input
# ---------------------------------------------------------------------------


def test_empty_probe_data():
    s = summarize({"streams": [], "format": {}})
    assert s.video == []
    assert s.audio == []
    assert s.subtitle == []


def test_audio_bitrate_none_when_no_fallback():
    data = {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "truehd",
                "disposition": {},
                "tags": {},
                # no bit_rate, format also has no bit_rate
            }
        ],
        "format": {},  # no bit_rate
    }
    s = summarize(data)
    assert s.audio[0].bitrate is None


def test_audio_channels_none_when_absent():
    data = {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "ac3",
                "disposition": {},
                "tags": {},
                "bit_rate": "448000",
                # no "channels" key
            }
        ],
        "format": {},
    }
    s = summarize(data)
    assert s.audio[0].channels is None
