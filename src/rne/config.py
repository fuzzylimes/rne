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
DEFAULT_AUDIO_CODEC = "copy"

FFPROBE_TIMEOUT = 30
FFPROBE_DEEP_TIMEOUT = 60

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8500
