import pytest

from rne.models import AudioTrack, HandbrakeArgs


# ---------------------------------------------------------------------------
# AudioTrack validation
# ---------------------------------------------------------------------------

def test_audio_track_copy_valid():
    t = AudioTrack(track=1, codec="copy")
    assert t.track == 1
    assert t.codec == "copy"
    assert t.bitrate is None


def test_audio_track_transcode_valid():
    t = AudioTrack(track=1, codec="ac3", bitrate=640)
    assert t.bitrate == 640


def test_audio_track_copy_with_bitrate_raises():
    with pytest.raises(ValueError, match="bitrate must be None"):
        AudioTrack(track=1, codec="copy", bitrate=640)


def test_audio_track_transcode_missing_bitrate_raises():
    with pytest.raises(ValueError, match="bitrate must be a positive int"):
        AudioTrack(track=1, codec="ac3", bitrate=None)


def test_audio_track_transcode_zero_bitrate_raises():
    with pytest.raises(ValueError, match="bitrate must be a positive int"):
        AudioTrack(track=1, codec="ac3", bitrate=0)


def test_audio_track_negative_track_raises():
    with pytest.raises(ValueError, match="track must be a positive int"):
        AudioTrack(track=0, codec="copy")


def test_audio_track_negative_index_raises():
    with pytest.raises(ValueError, match="track must be a positive int"):
        AudioTrack(track=-1, codec="copy")


# ---------------------------------------------------------------------------
# HandbrakeArgs round-trip
# ---------------------------------------------------------------------------

def test_round_trip_copy_only():
    args = HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="copy")])
    restored = HandbrakeArgs.from_json(args.to_json())
    assert restored.audio_tracks[0].track == 1
    assert restored.audio_tracks[0].codec == "copy"
    assert restored.audio_tracks[0].bitrate is None


def test_round_trip_transcode_only():
    args = HandbrakeArgs(audio_tracks=[AudioTrack(track=1, codec="ac3", bitrate=640)])
    restored = HandbrakeArgs.from_json(args.to_json())
    assert restored.audio_tracks[0].codec == "ac3"
    assert restored.audio_tracks[0].bitrate == 640


def test_round_trip_mixed():
    args = HandbrakeArgs(
        audio_tracks=[
            AudioTrack(track=1, codec="ac3", bitrate=640),
            AudioTrack(track=2, codec="copy"),
        ]
    )
    restored = HandbrakeArgs.from_json(args.to_json())
    assert len(restored.audio_tracks) == 2
    assert restored.audio_tracks[0].codec == "ac3"
    assert restored.audio_tracks[0].bitrate == 640
    assert restored.audio_tracks[1].codec == "copy"
    assert restored.audio_tracks[1].bitrate is None


def test_round_trip_preserves_other_fields():
    args = HandbrakeArgs(
        encoder="x264",
        quality=18,
        preset="fast",
        audio_tracks=[AudioTrack(track=1, codec="copy")],
        subtitle_tracks=[2, 3],
        decomb=True,
        extra_args=["--crop", "0:0:0:0"],
    )
    restored = HandbrakeArgs.from_json(args.to_json())
    assert restored.encoder == "x264"
    assert restored.quality == 18
    assert restored.preset == "fast"
    assert restored.subtitle_tracks == [2, 3]
    assert restored.decomb is True
    assert restored.extra_args == ["--crop", "0:0:0:0"]


def test_from_json_rejects_old_list_of_ints():
    old_json = '{"encoder":"x265","quality":20,"preset":"slow","audio_tracks":[1,2],"subtitle_tracks":[],"decomb":false,"extra_args":[]}'
    with pytest.raises(ValueError, match="list of track objects"):
        HandbrakeArgs.from_json(old_json)
