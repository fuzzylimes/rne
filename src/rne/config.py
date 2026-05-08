import os
from pathlib import Path

DB_PATH: str = os.environ.get(
    "RNE_DB",
    str(Path.home() / ".local/state/rne/jobs.db"),
)

MEDIA_ROOT = "/mnt/media"
STAGING_ROOT = "/mnt/media/staging"

# flatpak run --command=HandBrakeCLI fr.handbrake.ghb <args>
HANDBRAKE_PREFIX = ["flatpak", "run", "--command=HandBrakeCLI", "fr.handbrake.ghb"]

DEFAULT_ENCODER = "x265"
DEFAULT_QUALITY = 20
DEFAULT_PRESET = "slow"
DEFAULT_TUNE: str | None = None
DEFAULT_AUDIO_CODEC = "copy"

FFPROBE_TIMEOUT = 60  # standard probe only; rne probe --deep has no timeout

# Spec: "Audio codec policy" — codecs that play universally on Jellyfin clients
# and can be muxed as-is without transcoding.
COPY_FRIENDLY_AUDIO_CODECS: frozenset[str] = frozenset(
    {"ac3", "eac3", "aac", "mp3", "opus"}
)

# Spec: "Audio codec policy" — recommended AC3 bitrates by channel count.
AC3_BITRATE_BY_CHANNELS: dict[int, int] = {
    1: 96,
    2: 192,
    6: 640,  # 5.1
    8: 640,  # 7.1 (AC3 max)
}

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8500
