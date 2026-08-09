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

# Defaults for the optional [notifications.mqtt] table; see load_notify_config().
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_PAYLOAD = "{title} rip complete"
DEFAULT_MQTT_TIMEOUT = 5.0

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

_TOP_LEVEL_KEYS = frozenset({"default_disk", "disks", "notifications"})
_DISK_KEYS = frozenset({"media_root", "staging_root"})


def _read_toml(config_file: Path) -> dict | None:
    """Parse the config file. Returns None when the file does not exist."""
    try:
        with config_file.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"{config_file}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_file}: {exc}") from exc


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

    data = _read_toml(config_file)
    if data is None:
        return _EMPTY_DISK_CONFIG

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


# ---------------------------------------------------------------------------
# Notifications
#
# An optional [notifications.mqtt] table. Publishing "the disc is done" to an
# MQTT broker is entirely opt-in: with no table, every command behaves exactly
# as it did before. Topic and payload are user-owned strings — rne imposes no
# schema on them beyond {placeholder} substitution (see notify.render).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MqttConfig:
    """A resolved [notifications.mqtt] table, complete enough to publish with."""

    host: str
    topic: str
    payload: str = DEFAULT_MQTT_PAYLOAD
    port: int = DEFAULT_MQTT_PORT
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    qos: int = 0
    retain: bool = False
    tls: bool = False
    tls_insecure: bool = False
    timeout: float = DEFAULT_MQTT_TIMEOUT


@dataclass(frozen=True)
class NotifyConfig:
    """Notification settings for one invocation.

    `mqtt` is None when notifications are not set up. `skipped` carries a
    human-readable reason in the one case worth mentioning out loud: the table
    exists but is incomplete. Both None means there was nothing to configure.
    """

    mqtt: MqttConfig | None = None
    skipped: str | None = None


_EMPTY_NOTIFY_CONFIG = NotifyConfig()

_NOTIFY_KEYS = frozenset({"mqtt"})
_MQTT_KEYS = frozenset(
    {
        "host", "port", "topic", "payload", "username", "password",
        "client_id", "qos", "retain", "tls", "tls_insecure", "timeout",
    }
)
# Everything else has a usable default; without these two there is nowhere to
# publish and nothing to publish to.
_MQTT_REQUIRED = ("host", "topic")


def _mqtt_str(raw: dict, key: str) -> str | None:
    """Read an optional string key. Returns None when absent."""
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str):
        raise ConfigError(f"[notifications.mqtt]: {key} must be a string")
    if not value:
        raise ConfigError(f"[notifications.mqtt]: {key} must not be empty")
    return value


def _mqtt_bool(raw: dict, key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"[notifications.mqtt]: {key} must be true or false")
    return value


def _mqtt_int(raw: dict, key: str, default: int, *, low: int, high: int) -> int:
    if key not in raw:
        return default
    value = raw[key]
    # bool is an int subclass, so 'port = true' would otherwise read as port 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[notifications.mqtt]: {key} must be an integer")
    if not low <= value <= high:
        raise ConfigError(
            f"[notifications.mqtt]: {key} must be between {low} and {high} "
            f"(got {value})"
        )
    return value


def _mqtt_timeout(raw: dict, key: str, default: float) -> float:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[notifications.mqtt]: {key} must be a number")
    if value <= 0:
        raise ConfigError(f"[notifications.mqtt]: {key} must be greater than 0")
    return float(value)


def _parse_mqtt(raw: object) -> NotifyConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[notifications.mqtt] must be a table")

    unknown = sorted(set(raw) - _MQTT_KEYS)
    if unknown:
        raise ConfigError(
            f"[notifications.mqtt]: unknown key(s): {', '.join(unknown)}"
        )

    # A missing key means "not set up yet" and is not an error — the caller
    # skips notifying and carries on. A *wrong* key or a wrong type is a typo,
    # and staying quiet about it would mean never being notified and never
    # finding out why, which is the exact problem notifications exist to solve.
    missing = [key for key in _MQTT_REQUIRED if key not in raw]
    if missing:
        return NotifyConfig(
            skipped="[notifications.mqtt] is missing "
                    + ", ".join(f"'{key}'" for key in missing)
        )

    host = raw["host"]
    if not isinstance(host, str) or not host:
        raise ConfigError("[notifications.mqtt]: host must be a non-empty string")

    topic = raw["topic"]
    if not isinstance(topic, str) or not topic:
        raise ConfigError("[notifications.mqtt]: topic must be a non-empty string")
    if "+" in topic or "#" in topic:
        raise ConfigError(
            "[notifications.mqtt]: topic must not contain the wildcards "
            "'+' or '#' (those are for subscribing, not publishing)"
        )

    if "password" in raw and "username" not in raw:
        raise ConfigError(
            "[notifications.mqtt]: password is set without a username"
        )

    return NotifyConfig(
        mqtt=MqttConfig(
            host=host,
            topic=topic,
            payload=_mqtt_str(raw, "payload") or DEFAULT_MQTT_PAYLOAD,
            port=_mqtt_int(raw, "port", DEFAULT_MQTT_PORT, low=1, high=65535),
            username=_mqtt_str(raw, "username"),
            password=_mqtt_str(raw, "password"),
            client_id=_mqtt_str(raw, "client_id"),
            # QoS 2's four-packet handshake buys nothing for a fire-and-forget
            # notification, so the publisher implements 0 and 1 only.
            qos=_mqtt_int(raw, "qos", 0, low=0, high=1),
            retain=_mqtt_bool(raw, "retain", False),
            tls=_mqtt_bool(raw, "tls", False),
            tls_insecure=_mqtt_bool(raw, "tls_insecure", False),
            timeout=_mqtt_timeout(raw, "timeout", DEFAULT_MQTT_TIMEOUT),
        )
    )


def load_notify_config(path: str | Path | None = None) -> NotifyConfig:
    """Load the [notifications] section of the config file.

    A missing file, a missing section, or missing keys within it all yield a
    NotifyConfig with `mqtt=None` — notifications are opt-in, and not opting in
    is not an error. A section that exists but is malformed raises ConfigError
    so the mistake surfaces at startup, before the disc starts spinning.
    """
    config_file = Path(path) if path is not None else Path(CONFIG_PATH)

    data = _read_toml(config_file)
    if data is None:
        return _EMPTY_NOTIFY_CONFIG

    raw_notify = data.get("notifications")
    if raw_notify is None:
        return _EMPTY_NOTIFY_CONFIG
    if not isinstance(raw_notify, dict):
        raise ConfigError(f"{config_file}: 'notifications' must be a table")

    unknown = sorted(set(raw_notify) - _NOTIFY_KEYS)
    if unknown:
        raise ConfigError(
            f"{config_file}: [notifications]: unknown key(s): "
            f"{', '.join(unknown)} (supported transports: mqtt)"
        )

    if "mqtt" not in raw_notify:
        return _EMPTY_NOTIFY_CONFIG

    try:
        return _parse_mqtt(raw_notify["mqtt"])
    except ConfigError as exc:
        raise ConfigError(f"{config_file}: {exc}") from None


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
