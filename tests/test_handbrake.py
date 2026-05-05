import pytest

from rne import config
from rne.handbrake import build_command
from rne.models import HandbrakeArgs

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
    assert _flag_value(result, "--aencoder") == "copy"
    assert not _has_flag(result, "-s")
    assert _has_flag(result, "--markers")
    assert _has_flag(result, "--align-av")
    assert not _has_flag(result, "--decomb")


# ---------------------------------------------------------------------------
# Audio tracks
# ---------------------------------------------------------------------------

def test_multiple_audio_tracks():
    result = cmd(audio_tracks=[1, 2, 3])
    assert _flag_value(result, "-a") == "1,2,3"


def test_multiple_audio_tracks_aencoder_matches_count():
    result = cmd(audio_tracks=[1, 2], audio_codec="copy")
    assert _flag_value(result, "--aencoder") == "copy,copy"


def test_audio_codec_copy():
    result = cmd(audio_tracks=[1, 2], audio_codec="copy")
    assert _flag_value(result, "--aencoder") == "copy,copy"


def test_audio_codec_ac3():
    result = cmd(audio_tracks=[1, 2], audio_codec="ac3")
    assert _flag_value(result, "--aencoder") == "ac3,ac3"


# ---------------------------------------------------------------------------
# Subtitle tracks
# ---------------------------------------------------------------------------

def test_multiple_subtitle_tracks():
    result = cmd(subtitle_tracks=[1, 2])
    assert _flag_value(result, "-s") == "1,2"


def test_empty_subtitle_tracks_no_flag():
    result = cmd(subtitle_tracks=[])
    assert not _has_flag(result, "-s")


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
    assert result[-1] in ("--align-av", "--decomb")  # no junk appended


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
