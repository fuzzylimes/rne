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
