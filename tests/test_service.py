from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, call

import pytest

import rne.cli.service as svc


FAKE_WORKER = "/fake/bin/rne-worker"
FAKE_DASHBOARD = "/fake/bin/rne-dashboard"


def _which_side_effect(name):
    return {
        "rne-worker": FAKE_WORKER,
        "rne-dashboard": FAKE_DASHBOARD,
    }.get(name)


def test_install_substitutes_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("shutil.which", _which_side_effect)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

    svc.install()

    unit_dir = tmp_path / ".config" / "systemd" / "user"

    worker_text = (unit_dir / "rne-worker.service").read_text()
    assert f"ExecStart={FAKE_WORKER}" in worker_text
    assert "__RNE_WORKER_BIN__" not in worker_text
    assert "Restart=on-failure" in worker_text
    assert "Nice=10" in worker_text
    assert "WantedBy=multi-user.target" in worker_text

    dashboard_text = (unit_dir / "rne-dashboard.service").read_text()
    assert f"ExecStart={FAKE_DASHBOARD}" in dashboard_text
    assert "__RNE_DASHBOARD_BIN__" not in dashboard_text
    assert "Restart=on-failure" in dashboard_text
    assert "WantedBy=multi-user.target" in dashboard_text


def test_install_calls_daemon_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("shutil.which", _which_side_effect)

    calls = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kw: calls.append(args),
    )

    svc.install()

    assert calls.count(["systemctl", "--user", "daemon-reload"]) == 1


def test_install_exits_when_worker_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "rne-worker" else FAKE_DASHBOARD)
    with pytest.raises(SystemExit, match="rne-worker"):
        svc.install()


def test_install_exits_when_dashboard_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: FAKE_WORKER if name == "rne-worker" else None)
    with pytest.raises(SystemExit, match="rne-dashboard"):
        svc.install()


def test_uninstall_noop_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    reload_calls = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kw: reload_calls.append(args),
    )

    # Should not raise even though unit files don't exist
    svc.uninstall()

    assert ["systemctl", "--user", "daemon-reload"] in reload_calls


def test_uninstall_removes_existing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    worker_unit = unit_dir / "rne-worker.service"
    dashboard_unit = unit_dir / "rne-dashboard.service"
    worker_unit.write_text("dummy")
    dashboard_unit.write_text("dummy")

    svc.uninstall()

    assert not worker_unit.exists()
    assert not dashboard_unit.exists()
