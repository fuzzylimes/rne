import pytest

from rne import config
from rne.handbrake import build_command
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack

SRC = "/staging/title_t00.mkv"
OUT = "/staging/Movie.mkv"


def cmd(**kwargs) -> list[str]:
    return build_command(SRC, OUT, HandbrakeArgs(**kwargs))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flag_value(cmd: list[str], flag: str) -> str | None:
    """Return the token after `flag` in cmd, or None if flag is absent."""
    try:
        return cmd[cmd.index(flag) + 1]
    except ValueError:
        return None


def _has_flag(cmd: list[str], flag: str) -> bool:
    return flag in cmd


# ---------------------------------------------------------------------------
# Prefix and structure
# ---------------------------------------------------------------------------


def test_prefix():
    result = cmd()
    assert result[: len(config.HANDBRAKE_PREFIX)] == config.HANDBRAKE_PREFIX


def test_input_output():
    result = cmd()
    assert _flag_value(result, "-i") == SRC
    assert _flag_value(result, "-o") == OUT


# ---------------------------------------------------------------------------
# Default args produce a sensible command
# ---------------------------------------------------------------------------


def test_defaults():
    result = cmd()
    assert _flag_value(result, "--encoder") == "x265"
    assert _flag_value(result, "--quality") == "20"
    assert _flag_value(result, "--encoder-preset") == "slow"
    assert _flag_value(result, "-a") == "1"
    assert _flag_value(result, "-E") == "copy"
    assert _flag_value(result, "-B") is None
    assert not _has_flag(result, "-s")
    assert _has_flag(result, "--markers")
    assert _has_flag(result, "--align-av")
    assert not _has_flag(result, "--decomb")


# ---------------------------------------------------------------------------
# Audio tracks — parallel -a / -E / -B lists
# ---------------------------------------------------------------------------


def test_single_copy_track():
    result = cmd(audio_tracks=[AudioTrack(track=1, codec="copy")])
    assert _flag_value(result, "-a") == "1"
    assert _flag_value(result, "-E") == "copy"
    assert _flag_value(result, "-B") is None


def test_single_transcode_track():
    result = cmd(audio_tracks=[AudioTrack(track=1, codec="ac3", bitrate=640)])
    assert _flag_value(result, "-a") == "1"
    assert _flag_value(result, "-E") == "ac3"
    assert _flag_value(result, "-B") == "640"


def test_mixed_copy_and_transcode():
    tracks = [
        AudioTrack(track=1, codec="ac3", bitrate=640),
        AudioTrack(track=2, codec="copy"),
    ]
    result = cmd(audio_tracks=tracks)
    assert _flag_value(result, "-a") == "1,2"
    assert _flag_value(result, "-E") == "ac3,copy"
    assert _flag_value(result, "-B") == "640,"


def test_multiple_transcode_tracks():
    tracks = [
        AudioTrack(track=1, codec="ac3", bitrate=640),
        AudioTrack(track=2, codec="aac", bitrate=192),
        AudioTrack(track=3, codec="ac3", bitrate=96),
    ]
    result = cmd(audio_tracks=tracks)
    assert _flag_value(result, "-a") == "1,2,3"
    assert _flag_value(result, "-E") == "ac3,aac,ac3"
    assert _flag_value(result, "-B") == "640,192,96"


def test_audio_order_preserved_high_first():
    tracks = [
        AudioTrack(track=4, codec="copy"),
        AudioTrack(track=2, codec="ac3", bitrate=192),
    ]
    result = cmd(audio_tracks=tracks)
    assert _flag_value(result, "-a") == "4,2"
    assert _flag_value(result, "-E") == "copy,ac3"
    assert _flag_value(result, "-B") == ",192"


def test_audio_order_preserved_reversed():
    tracks = [
        AudioTrack(track=2, codec="ac3", bitrate=192),
        AudioTrack(track=4, codec="copy"),
    ]
    result = cmd(audio_tracks=tracks)
    assert _flag_value(result, "-a") == "2,4"
    assert _flag_value(result, "-E") == "ac3,copy"
    assert _flag_value(result, "-B") == "192,"


# ---------------------------------------------------------------------------
# Subtitle tracks
# ---------------------------------------------------------------------------


def test_multiple_subtitle_tracks():
    result = cmd(subtitle_tracks=[SubtitleTrack(1), SubtitleTrack(2)])
    assert _flag_value(result, "-s") == "1,2"


def test_empty_subtitle_tracks_no_flag():
    result = cmd(subtitle_tracks=[])
    assert not _has_flag(result, "-s")


def test_subtitle_default_flag_emitted():
    result = cmd(subtitle_tracks=[SubtitleTrack(1), SubtitleTrack(2, default=True)])
    assert _flag_value(result, "--subtitle-default") == "2"


def test_subtitle_default_first_track():
    result = cmd(subtitle_tracks=[SubtitleTrack(3, default=True), SubtitleTrack(1)])
    assert _flag_value(result, "--subtitle-default") == "1"


def test_subtitle_default_omitted_when_none():
    result = cmd(subtitle_tracks=[SubtitleTrack(1), SubtitleTrack(2)])
    assert not _has_flag(result, "--subtitle-default")


def test_subtitle_multiple_defaults_raises():
    with pytest.raises(ValueError, match="at most one"):
        cmd(subtitle_tracks=[SubtitleTrack(1, default=True), SubtitleTrack(2, default=True)])


# ---------------------------------------------------------------------------
# Decomb
# ---------------------------------------------------------------------------


def test_decomb_true():
    result = cmd(decomb=True)
    assert _has_flag(result, "--decomb")


def test_decomb_false():
    result = cmd(decomb=False)
    assert not _has_flag(result, "--decomb")


# ---------------------------------------------------------------------------
# extra_args appended at the end
# ---------------------------------------------------------------------------


def test_extra_args_appended():
    extra = ["--crop", "0:0:0:0"]
    result = cmd(extra_args=extra)
    assert result[-2:] == extra


def test_extra_args_empty():
    result = cmd(extra_args=[])
    assert result[-1] in ("--align-av", "--decomb")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_empty_audio_tracks_raises():
    with pytest.raises(ValueError, match="audio_tracks"):
        cmd(audio_tracks=[])


def test_negative_quality_raises():
    with pytest.raises(ValueError, match="quality"):
        cmd(quality=-1)


def test_unknown_encoder_raises():
    with pytest.raises(ValueError, match="encoder"):
        cmd(encoder="notareal265")


def test_zero_quality_is_valid():
    result = cmd(quality=0)
    assert _flag_value(result, "--quality") == "0"


# ---------------------------------------------------------------------------
# encoder-tune
# ---------------------------------------------------------------------------


def test_encoder_tune_animation():
    result = cmd(tune="animation")
    assert _flag_value(result, "--encoder-tune") == "animation"


def test_encoder_tune_omitted_when_none():
    result = cmd(tune=None)
    assert not _has_flag(result, "--encoder-tune")


def test_encoder_tune_custom_value():
    result = cmd(tune="grain")
    assert _flag_value(result, "--encoder-tune") == "grain"
