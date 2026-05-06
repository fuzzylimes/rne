from __future__ import annotations

import importlib.resources
import shutil
import subprocess
import sys
from pathlib import Path


_UNITS = {
    "rne-worker.service": ("rne-worker", "__RNE_WORKER_BIN__"),
    "rne-dashboard.service": ("rne-dashboard", "__RNE_DASHBOARD_BIN__"),
}


def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def install() -> None:
    worker_bin = shutil.which("rne-worker")
    if worker_bin is None:
        sys.exit("rne-worker not found on PATH; is the package installed?")

    dashboard_bin = shutil.which("rne-dashboard")
    if dashboard_bin is None:
        sys.exit("rne-dashboard not found on PATH; is the package installed?")

    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)

    pkg = importlib.resources.files("rne.systemd")
    for unit_name in _UNITS:
        content = pkg.joinpath(unit_name).read_text()
        content = content.replace(
            "__RNE_WORKER_BIN__", worker_bin
        ).replace(
            "__RNE_DASHBOARD_BIN__", dashboard_bin
        )
        target = unit_dir / unit_name
        target.write_text(content)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    print(f"Unit files written to: {unit_dir}")
    print(f"  rne-worker  -> {worker_bin}")
    print(f"  rne-dashboard -> {dashboard_bin}")
    print()
    print("Next step:")
    print("  systemctl --user enable --now rne-worker rne-dashboard")


def uninstall() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "disable", "--now", "rne-worker", "rne-dashboard"],
        check=False,
    )
    if result is not None and result.returncode != 0:
        print(f"systemctl disable returned {result.returncode} (units may not have been loaded)")

    unit_dir = _unit_dir()
    removed = []
    for unit_name in _UNITS:
        target = unit_dir / unit_name
        if target.exists():
            target.unlink(missing_ok=True)
            removed.append(str(target))

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    if removed:
        print("Removed:")
        for path in removed:
            print(f"  {path}")
    else:
        print("No unit files found to remove.")
