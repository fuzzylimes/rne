import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DB_PATH: str = os.environ.get(
    "RNE_DB",
    str(Path.home() / ".local/state/rne/jobs.db"),
)

MEDIA_ROOT = "/mnt/media"
STAGING_ROOT: str = os.environ.get("RNE_STAGING_ROOT", "/mnt/media/staging")

# Optional TOML file defining named disks; see resolve_roots().
CONFIG_PATH: str = os.environ.get(
    "RNE_CONFIG",
    str(Path.home() / ".config/rne/config.toml"),
)

# flatpak run --command=HandBrakeCLI fr.handbrake.ghb <args>
HANDBRAKE_PREFIX = ["flatpak", "run", "--command=HandBrakeCLI", "fr.handbrake.ghb"]

DEFAULT_ENCODER = "x265"
DEFAULT_QUALITY = 20
DEFAULT_PRESET = "slow"
# DVD sources encode fast enough that "slow" buys little; default to "medium".
DEFAULT_PRESET_DVD = "medium"
DEFAULT_TUNE: str | None = None
DEFAULT_AUDIO_CODEC = "copy"


def _rip_retries() -> int:
    """Automatic retries when ripping a title fails (RNE_RIP_RETRIES env var).

    Default 1; clamped to 0-10.
    """
    try:
        value = int(os.environ.get("RNE_RIP_RETRIES", "1"))
    except ValueError:
        return 1
    return max(0, min(value, 10))


RIP_RETRIES: int = _rip_retries()

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


# ---------------------------------------------------------------------------
# Named disks
#
# Everything above is a flat constant resolved at import time. Output location
# is the exception: it varies per invocation now that there is more than one
# drive to write to, so it is resolved from flags + an optional TOML file.
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """The config file exists but is unusable, or a named disk is unknown."""


@dataclass(frozen=True)
class Roots:
    """Resolved output locations for a single ingest/queue invocation."""

    media_root: Path
    staging_root: Path


@dataclass(frozen=True)
class DiskConfig:
    """Parsed contents of the config file."""

    default_disk: str | None
    disks: dict[str, Roots]


_EMPTY_DISK_CONFIG = DiskConfig(default_disk=None, disks={})

_TOP_LEVEL_KEYS = frozenset({"default_disk", "disks"})
_DISK_KEYS = frozenset({"media_root", "staging_root"})


def default_roots() -> Roots:
    """Built-in roots, honouring the RNE_STAGING_ROOT env var."""
    return Roots(media_root=Path(MEDIA_ROOT), staging_root=Path(STAGING_ROOT))


def _disk_path(value: object, *, disk: str, key: str) -> Path:
    if not isinstance(value, str):
        raise ConfigError(f"disk {disk!r}: {key} must be a string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(
            f"disk {disk!r}: {key} must be an absolute path (got {value!r})"
        )
    return path


def _parse_disk(name: str, raw: object) -> Roots:
    if not isinstance(raw, dict):
        raise ConfigError(f"disk {name!r} must be a table")
    # Unknown keys are rejected rather than ignored: a typo'd 'stageing_root'
    # would otherwise silently fall back to the derived default.
    unknown = sorted(set(raw) - _DISK_KEYS)
    if unknown:
        raise ConfigError(f"disk {name!r}: unknown key(s): {', '.join(unknown)}")
    if "media_root" not in raw:
        raise ConfigError(f"disk {name!r} is missing required key 'media_root'")

    media_root = _disk_path(raw["media_root"], disk=name, key="media_root")
    if "staging_root" in raw:
        staging_root = _disk_path(raw["staging_root"], disk=name, key="staging_root")
    else:
        staging_root = media_root / "staging"
    return Roots(media_root=media_root, staging_root=staging_root)


def load_disk_config(path: str | Path | None = None) -> DiskConfig:
    """Load and validate the config file.

    A missing file is not an error — it yields an empty config and callers fall
    back to the built-in defaults. A file that exists but cannot be parsed or
    validated raises ConfigError instead of being silently ignored, so a typo
    never quietly sends an encode to the wrong drive.
    """
    config_file = Path(path) if path is not None else Path(CONFIG_PATH)

    try:
        with config_file.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return _EMPTY_DISK_CONFIG
    except OSError as exc:
        raise ConfigError(f"{config_file}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_file}: {exc}") from exc

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(
            f"{config_file}: unknown top-level key(s): {', '.join(unknown)}"
        )

    raw_disks = data.get("disks", {})
    if not isinstance(raw_disks, dict):
        raise ConfigError(f"{config_file}: 'disks' must be a table")

    disks: dict[str, Roots] = {}
    for name, raw in raw_disks.items():
        try:
            disks[name] = _parse_disk(name, raw)
        except ConfigError as exc:
            raise ConfigError(f"{config_file}: {exc}") from None

    default_disk = data.get("default_disk")
    if default_disk is not None:
        if not isinstance(default_disk, str):
            raise ConfigError(f"{config_file}: 'default_disk' must be a string")
        if default_disk not in disks:
            known = ", ".join(sorted(disks)) or "none"
            raise ConfigError(
                f"{config_file}: default_disk {default_disk!r} is not defined "
                f"under [disks] (defined: {known})"
            )

    return DiskConfig(default_disk=default_disk, disks=disks)


def resolve_roots(
    *,
    disk: str | None = None,
    media_root: str | None = None,
    config_path: str | Path | None = None,
) -> Roots:
    """Resolve the output roots for one invocation.

    Precedence: an explicit media_root (`--media-root`), then a named disk
    (`--disk`), then the config file's default_disk, then the built-in
    defaults. `disk` and `media_root` are mutually exclusive at the CLI level.
    """
    if media_root is not None:
        root = Path(media_root).expanduser().resolve()
        return Roots(media_root=root, staging_root=root / "staging")

    cfg = load_disk_config(config_path)

    if disk is not None:
        try:
            return cfg.disks[disk]
        except KeyError:
            known = ", ".join(sorted(cfg.disks)) or "none"
            raise ConfigError(
                f"unknown disk {disk!r} (configured: {known}). Define it in "
                f"{config_path or CONFIG_PATH}, or pass --media-root instead."
            ) from None

    if cfg.default_disk is not None:
        return cfg.disks[cfg.default_disk]

    return default_roots()
