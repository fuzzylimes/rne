# rne — VM Installation Runbook

Target: `rip` VM at `rip.lan`, Ubuntu 24.04, user `rip`.

---

## a. Prerequisites

Verify each dependency on the VM before proceeding.

```bash
# Python 3.12+
python3 --version

# MakeMKV (must be licensed)
makemkvcon --version

# HandBrake (Flatpak)
flatpak list | grep handbrake

# ffprobe
ffprobe -version

# pipx
pipx --version
```

If `pipx` is missing:

```bash
sudo apt install pipx && pipx ensurepath
# Then re-login or: source ~/.bashrc
```

---

## b. Install

On the Mac (build machine):

```bash
uv build
# Produces dist/rne-0.1.0-py3-none-any.whl
rsync -av dist/rne-0.1.0-py3-none-any.whl rip@rip.lan:~/
```

On the VM:

```bash
pipx install ~/rne-0.1.0-py3-none-any.whl
```

Verify all three entry points landed in `~/.local/bin/`:

```bash
which rne rne-worker rne-dashboard
```

---

## c. Service installation

Install the systemd unit files (substitutes the correct binary paths automatically):

```bash
rne service install
```

Enable linger so user services start at boot without an active login session
(requires sudo — intentionally kept outside `rne service install`):

```bash
loginctl enable-linger rip
```

Enable and start both services:

```bash
systemctl --user enable --now rne-worker rne-dashboard
```

---

## d. Verify

```bash
systemctl --user status rne-worker rne-dashboard
```

Both should show `active (running)`.

Confirm the database was created on first worker start:

```bash
ls ~/.local/state/rne/jobs.db
```

Check that the expected tables exist:

```bash
sqlite3 ~/.local/state/rne/jobs.db ".tables"
```

Should list: `ingest_batches  jobs  queue_settings  worker_status`

If `worker_status` is missing, `db.init()` did not run — check the worker logs:

```bash
journalctl --user -u rne-worker -n 100
```

**Verify the heartbeat — dashboard (preferred):**

Open the dashboard in a browser:

```
http://rip.lan:8500/
```

The header should show `Worker: idle • Ns ago` with a small N. The page auto-refreshes every 30 seconds — watch the N increase and reset between refreshes. If N stays small and ticking, the worker is alive.

**Verify the heartbeat — direct DB check (headless or pre-DNS):**

```bash
sqlite3 ~/.local/state/rne/jobs.db \
  "SELECT pid, state, datetime(last_seen, 'localtime') FROM worker_status;"
```

Run this twice roughly 15 seconds apart. `last_seen` should advance and `state` should be `idle`. If `last_seen` does not change, the worker is not running.

> **Note:** The heartbeat is a silent DB write — the journal stays quiet during idle. Use the DB or dashboard checks above, not `journalctl -f`, to confirm the heartbeat is firing.

Use `journalctl` to watch for actual events (job claims, completions, failures, HandBrake stderr):

```bash
journalctl --user -u rne-worker -f
```

---

## e. DNS convenience

Add `rip.lan rne.lan` to Pi-hole local DNS. Dashboard then reachable at:

```
http://rne.lan:8500/
```

---

## f. Update workflow

On the Mac:

```bash
uv build
rsync -av dist/rne-<version>.whl rip@rip.lan:~/
```

On the VM:

```bash
pipx install --force ~/rne-<version>.whl
rne service install          # only needed when unit files changed
systemctl --user restart rne-worker rne-dashboard
```

---

## g. Uninstall

```bash
rne service uninstall
pipx uninstall rne
```
