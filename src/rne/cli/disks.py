from __future__ import annotations

import sys

from rne import config
from rne.cli._pipeline import _print_table


def run() -> None:
    """Print the configured disks, or the built-in defaults if none are set."""
    try:
        cfg = config.load_disk_config()
    except config.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(2)

    if not cfg.disks:
        defaults = config.default_roots()
        print(f"No disks configured in {config.CONFIG_PATH}; using defaults:\n")
        print(f"  media root:   {defaults.media_root}")
        print(f"  staging root: {defaults.staging_root}")
        print(
            "\nDefine disks to switch between drives with `-dk NAME`. Example:\n"
            "\n  default_disk = \"media\"\n"
            "\n  [disks.media]\n"
            "  media_root = \"/mnt/media\"\n"
        )
        return

    print(f"{config.CONFIG_PATH}\n")
    rows = []
    for name in sorted(cfg.disks):
        roots = cfg.disks[name]
        rows.append(
            {
                "": "*" if name == cfg.default_disk else "",
                "Disk": name,
                "Media Root": str(roots.media_root),
                "Staging Root": str(roots.staging_root),
                "Mounted": "yes" if roots.media_root.is_dir() else "NO",
            }
        )
    _print_table(["", "Disk", "Media Root", "Staging Root", "Mounted"], rows)

    if cfg.default_disk is None:
        print("\nNo default_disk set; falling back to built-in defaults "
              f"({config.STAGING_ROOT}) when -dk is omitted.")
    else:
        print("\n* = default (used when -dk is omitted)")
