"""Named-disk config loading and root resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from rne import config
from rne.cli import _build_parser


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# load_disk_config
# ---------------------------------------------------------------------------


def test_missing_file_is_not_an_error(tmp_path):
    cfg = config.load_disk_config(tmp_path / "nope.toml")
    assert cfg.disks == {}
    assert cfg.default_disk is None


def test_empty_file_is_valid(tmp_path):
    cfg = config.load_disk_config(write_config(tmp_path, ""))
    assert cfg.disks == {}
    assert cfg.default_disk is None


def test_staging_root_derived_from_media_root(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        media_root = "/mnt/media"
    """)
    cfg = config.load_disk_config(path)
    assert cfg.disks["media"] == config.Roots(
        media_root=Path("/mnt/media"),
        staging_root=Path("/mnt/media/staging"),
    )


def test_staging_root_override_wins(tmp_path):
    path = write_config(tmp_path, """
        [disks.archive]
        media_root = "/mnt/media2"
        staging_root = "/mnt/scratch/incoming"
    """)
    roots = config.load_disk_config(path).disks["archive"]
    assert roots.media_root == Path("/mnt/media2")
    assert roots.staging_root == Path("/mnt/scratch/incoming")


def test_multiple_disks_and_default(tmp_path):
    path = write_config(tmp_path, """
        default_disk = "archive"

        [disks.media]
        media_root = "/mnt/media"

        [disks.archive]
        media_root = "/mnt/media2"
    """)
    cfg = config.load_disk_config(path)
    assert set(cfg.disks) == {"media", "archive"}
    assert cfg.default_disk == "archive"


def test_tilde_in_media_root_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_config(tmp_path, """
        [disks.home]
        media_root = "~/media"
    """)
    roots = config.load_disk_config(path).disks["home"]
    assert roots.media_root == tmp_path / "media"
    assert roots.staging_root == tmp_path / "media/staging"


# ---------------------------------------------------------------------------
# load_disk_config — malformed files are hard errors
# ---------------------------------------------------------------------------


def test_unparseable_toml_raises(tmp_path):
    path = write_config(tmp_path, "this is not = = toml")
    with pytest.raises(config.ConfigError):
        config.load_disk_config(path)


def test_missing_media_root_raises(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        staging_root = "/mnt/media/staging"
    """)
    with pytest.raises(config.ConfigError, match="media_root"):
        config.load_disk_config(path)


def test_typo_in_disk_key_raises(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        media_root = "/mnt/media"
        stageing_root = "/mnt/media/staging"
    """)
    with pytest.raises(config.ConfigError, match="stageing_root"):
        config.load_disk_config(path)


def test_unknown_top_level_key_raises(tmp_path):
    path = write_config(tmp_path, """
        default_disks = "media"

        [disks.media]
        media_root = "/mnt/media"
    """)
    with pytest.raises(config.ConfigError, match="default_disks"):
        config.load_disk_config(path)


def test_relative_media_root_raises(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        media_root = "media"
    """)
    with pytest.raises(config.ConfigError, match="absolute"):
        config.load_disk_config(path)


def test_non_string_media_root_raises(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        media_root = 42
    """)
    with pytest.raises(config.ConfigError, match="must be a string"):
        config.load_disk_config(path)


def test_default_disk_pointing_at_undefined_disk_raises(tmp_path):
    path = write_config(tmp_path, """
        default_disk = "archive"

        [disks.media]
        media_root = "/mnt/media"
    """)
    with pytest.raises(config.ConfigError, match="not defined"):
        config.load_disk_config(path)


def test_disk_that_is_not_a_table_raises(tmp_path):
    path = write_config(tmp_path, """
        [disks]
        media = "/mnt/media"
    """)
    with pytest.raises(config.ConfigError, match="must be a table"):
        config.load_disk_config(path)


# ---------------------------------------------------------------------------
# resolve_roots — precedence
# ---------------------------------------------------------------------------


def test_no_flags_no_config_uses_builtin_defaults(tmp_path):
    roots = config.resolve_roots(config_path=tmp_path / "nope.toml")
    assert roots == config.default_roots()
    assert roots.staging_root == Path(config.STAGING_ROOT)


def test_default_disk_used_when_no_flags(tmp_path):
    path = write_config(tmp_path, """
        default_disk = "archive"

        [disks.archive]
        media_root = "/mnt/media2"
    """)
    roots = config.resolve_roots(config_path=path)
    assert roots.staging_root == Path("/mnt/media2/staging")


def test_config_without_default_falls_back_to_builtin(tmp_path):
    path = write_config(tmp_path, """
        [disks.archive]
        media_root = "/mnt/media2"
    """)
    assert config.resolve_roots(config_path=path) == config.default_roots()


def test_disk_flag_selects_named_disk(tmp_path):
    path = write_config(tmp_path, """
        default_disk = "media"

        [disks.media]
        media_root = "/mnt/media"

        [disks.archive]
        media_root = "/mnt/media2"
    """)
    roots = config.resolve_roots(disk="archive", config_path=path)
    assert roots.media_root == Path("/mnt/media2")


def test_unknown_disk_flag_raises_and_lists_known_disks(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        media_root = "/mnt/media"
    """)
    with pytest.raises(config.ConfigError, match="unknown disk 'nas'.*media"):
        config.resolve_roots(disk="nas", config_path=path)


def test_unknown_disk_flag_with_no_config_raises(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown disk"):
        config.resolve_roots(disk="archive", config_path=tmp_path / "nope.toml")


def test_media_root_flag_beats_default_disk(tmp_path):
    path = write_config(tmp_path, """
        default_disk = "media"

        [disks.media]
        media_root = "/mnt/media"
    """)
    roots = config.resolve_roots(media_root="/mnt/elsewhere", config_path=path)
    assert roots.media_root == Path("/mnt/elsewhere")
    assert roots.staging_root == Path("/mnt/elsewhere/staging")


def test_media_root_flag_does_not_read_config(tmp_path):
    # A broken config must not block an explicit --media-root.
    path = write_config(tmp_path, "this is not = = toml")
    roots = config.resolve_roots(media_root="/mnt/elsewhere", config_path=path)
    assert roots.media_root == Path("/mnt/elsewhere")


def test_media_root_flag_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    roots = config.resolve_roots(media_root="~/media")
    assert roots.media_root == (tmp_path / "media").resolve()


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["ingest", "queue"])
def test_disk_flag_parsed_on_both_commands(command):
    extra = ["/some/path"] if command == "queue" else []
    args = _build_parser().parse_args([command, *extra, "-dk", "archive"])
    assert args.disk == "archive"
    assert args.media_root is None


@pytest.mark.parametrize("command", ["ingest", "queue"])
def test_media_root_flag_parsed_on_both_commands(command):
    extra = ["/some/path"] if command == "queue" else []
    args = _build_parser().parse_args([command, *extra, "--media-root", "/mnt/x"])
    assert args.media_root == "/mnt/x"
    assert args.disk is None


@pytest.mark.parametrize("command", ["ingest", "queue"])
def test_disk_flags_default_to_none(command):
    extra = ["/some/path"] if command == "queue" else []
    args = _build_parser().parse_args([command, *extra])
    assert args.disk is None
    assert args.media_root is None


def test_disk_and_media_root_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            ["ingest", "-dk", "archive", "--media-root", "/mnt/x"]
        )


def test_disks_command_parses():
    assert _build_parser().parse_args(["disks"]).command == "disks"


# ---------------------------------------------------------------------------
# load_notify_config — opting in
# ---------------------------------------------------------------------------


def test_no_config_file_means_no_notifications(tmp_path):
    cfg = config.load_notify_config(tmp_path / "nope.toml")
    assert cfg.mqtt is None
    assert cfg.skipped is None


def test_no_notifications_section_means_no_notifications(tmp_path):
    path = write_config(tmp_path, """
        [disks.media]
        media_root = "/mnt/media"
    """)
    cfg = config.load_notify_config(path)
    assert cfg.mqtt is None
    assert cfg.skipped is None


def test_minimal_mqtt_section_applies_defaults(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "homeassistant.local"
        topic = "rne/disc"
    """)
    mqtt = config.load_notify_config(path).mqtt
    assert mqtt == config.MqttConfig(
        host="homeassistant.local",
        topic="rne/disc",
        payload=config.DEFAULT_MQTT_PAYLOAD,
        port=1883,
        timeout=config.DEFAULT_MQTT_TIMEOUT,
    )


def test_full_mqtt_section_is_read_verbatim(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "10.0.0.5"
        port = 8883
        username = "rne"
        password = "hunter2"
        client_id = "ripper"
        topic = "rne/{hostname}/disc"
        payload = '{"disc": "{disc}"}'
        qos = 1
        retain = true
        tls = true
        tls_insecure = true
        timeout = 2
    """)
    mqtt = config.load_notify_config(path).mqtt
    assert mqtt == config.MqttConfig(
        host="10.0.0.5",
        topic="rne/{hostname}/disc",
        payload='{"disc": "{disc}"}',
        port=8883,
        username="rne",
        password="hunter2",
        client_id="ripper",
        qos=1,
        retain=True,
        tls=True,
        tls_insecure=True,
        timeout=2.0,
    )


def test_integer_timeout_becomes_float(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "h"
        topic = "t"
        timeout = 3
    """)
    assert config.load_notify_config(path).mqtt.timeout == 3.0


def test_notifications_coexist_with_disks(tmp_path):
    path = write_config(tmp_path, """
        default_disk = "media"

        [disks.media]
        media_root = "/mnt/media"

        [notifications.mqtt]
        host = "h"
        topic = "t"
    """)
    assert config.load_disk_config(path).default_disk == "media"
    assert config.load_notify_config(path).mqtt.host == "h"


# ---------------------------------------------------------------------------
# load_notify_config — missing keys skip, typos are hard errors
# ---------------------------------------------------------------------------


def test_empty_mqtt_section_skips_with_a_reason(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
    """)
    cfg = config.load_notify_config(path)
    assert cfg.mqtt is None
    assert "'host'" in cfg.skipped and "'topic'" in cfg.skipped


def test_missing_topic_skips_with_a_reason(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "homeassistant.local"
    """)
    cfg = config.load_notify_config(path)
    assert cfg.mqtt is None
    assert "'topic'" in cfg.skipped
    assert "'host'" not in cfg.skipped


def test_empty_notifications_table_is_not_an_error(tmp_path):
    path = write_config(tmp_path, """
        [notifications]
    """)
    cfg = config.load_notify_config(path)
    assert cfg.mqtt is None
    assert cfg.skipped is None


def test_typo_in_mqtt_key_raises(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "h"
        topic = "t"
        topc = "rne/disc"
    """)
    with pytest.raises(config.ConfigError, match="topc"):
        config.load_notify_config(path)


def test_unknown_transport_raises(tmp_path):
    path = write_config(tmp_path, """
        [notifications.webhook]
        url = "http://example.test/hook"
    """)
    with pytest.raises(config.ConfigError, match="webhook"):
        config.load_notify_config(path)


def test_notifications_section_is_accepted_by_load_disk_config(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "h"
        topic = "t"
    """)
    assert config.load_disk_config(path).disks == {}


@pytest.mark.parametrize(
    "body,match",
    [
        ('host = 42\ntopic = "t"', "host must be a non-empty string"),
        ('host = "h"\ntopic = ""', "topic must be a non-empty string"),
        ('host = "h"\ntopic = "rne/#"', "wildcards"),
        ('host = "h"\ntopic = "rne/+/disc"', "wildcards"),
        ('host = "h"\ntopic = "t"\nport = "1883"', "port must be an integer"),
        ('host = "h"\ntopic = "t"\nport = true', "port must be an integer"),
        ('host = "h"\ntopic = "t"\nport = 0', "between 1 and 65535"),
        ('host = "h"\ntopic = "t"\nport = 70000', "between 1 and 65535"),
        ('host = "h"\ntopic = "t"\nqos = 2', "qos must be between 0 and 1"),
        ('host = "h"\ntopic = "t"\nqos = -1', "qos must be between 0 and 1"),
        ('host = "h"\ntopic = "t"\nretain = "yes"', "retain must be true or false"),
        ('host = "h"\ntopic = "t"\ntls = 1', "tls must be true or false"),
        ('host = "h"\ntopic = "t"\ntimeout = 0', "greater than 0"),
        ('host = "h"\ntopic = "t"\ntimeout = "5"', "timeout must be a number"),
        ('host = "h"\ntopic = "t"\nusername = 7', "username must be a string"),
        ('host = "h"\ntopic = "t"\npayload = ""', "payload must not be empty"),
        ('host = "h"\ntopic = "t"\npassword = "p"', "without a username"),
    ],
)
def test_malformed_mqtt_values_raise(tmp_path, body, match):
    path = write_config(tmp_path, f"[notifications.mqtt]\n{body}\n")
    with pytest.raises(config.ConfigError, match=match):
        config.load_notify_config(path)


def test_mqtt_that_is_not_a_table_raises(tmp_path):
    path = write_config(tmp_path, """
        [notifications]
        mqtt = "homeassistant.local"
    """)
    with pytest.raises(config.ConfigError, match="must be a table"):
        config.load_notify_config(path)


def test_notifications_that_is_not_a_table_raises(tmp_path):
    path = write_config(tmp_path, 'notifications = "on"\n')
    with pytest.raises(config.ConfigError, match="must be a table"):
        config.load_notify_config(path)


def test_unparseable_toml_raises_for_notifications_too(tmp_path):
    path = write_config(tmp_path, "this is not = = toml")
    with pytest.raises(config.ConfigError):
        config.load_notify_config(path)


def test_config_error_message_names_the_file(tmp_path):
    path = write_config(tmp_path, """
        [notifications.mqtt]
        host = "h"
        topic = "t"
        qos = 2
    """)
    with pytest.raises(config.ConfigError, match=str(path)):
        config.load_notify_config(path)
