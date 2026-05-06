# Dashboard smoke test

## Setup

```bash
# Install the package in editable mode (if not already done)
uv pip install -e .

# Seed the DB with jobs in various states
RNE_DB=/tmp/rne_test.db uv run python tests/fixtures/seed_dashboard.py

# Start the dashboard — runs in the foreground, so use a second terminal
# or append & to background it
RNE_DB=/tmp/rne_test.db uv run python -m rne.dashboard.app

# Confirm it's up (in another terminal)
curl -s http://localhost:8500/ | head -3
```

> **Sandbox note**: `rne-dashboard` and bare `python` won't find the venv in this
> devcontainer. Always prefix with `uv run`. If `localhost:8500` doesn't open in
> your browser, check the VS Code **Ports** tab and forward port 8500.

Open http://localhost:8500 in a browser.

## Checks

1. **Header** — worker dot is green (state "encoding"), "Pause queue" and "Resume queue"
   buttons are visible.

2. **Running section** — Initial D S01E05 shows a progress bar at ~67 %, fps, and ETA.

3. **Queued section** — count "(3)" in the heading; S01E06 and S01E07 show Pause buttons;
   S01E08 shows a Resume button (it was seeded as paused).

4. **Recent section** — four rows: two "done" (S01E04, S01E02) with Re-encode buttons,
   one "failed" (S01E03) with a Retry button and the error snippet, one "interrupted"
   (The Silence of the Lambs) with a Retry button.

## Action tests

| Action | How | Expected result |
|--------|-----|-----------------|
| Pause a queued job | Click Pause next to S01E06 | Row moves to paused, button becomes Resume |
| Resume a paused job | Click Resume next to S01E08 | Row moves back to queued with Pause button |
| Retry a failed job | Click Retry next to S01E03 | Row disappears from Recent, reappears in Queued |
| Pause the queue | Click "Pause queue" | Banner "Queue paused" appears at top |
| Resume the queue | Click "Resume queue" | Banner disappears |

## Auto-refresh

Leave the page open for 30 seconds — it should reload automatically (observe the browser
loading indicator). While it's reloading, run the seed script again to confirm new state
appears after the next refresh cycle.

## Mobile

Resize the browser to < 600 px wide (or use DevTools device emulation):
- ETA column should disappear.
- Show/episode columns should collapse to a single line (`Initial D · S01E05` style).

---

# Manual Smoke Test — `rne ingest` (CLI)

Run on the `rip` VM (`192.168.50.176`) with a disc in the drive.

## Prerequisites

```bash
cd ~/rne
uv pip install -e .
rne --help   # should list: ingest, ls, edit, cancel, retry, pause, resume
```

## Step-by-step

### 1. Happy-path TV ingest (Blu-ray)

```
rne ingest
```

| Step | Expected |
|------|----------|
| Disc detection | `makemkvcon -r --minlength=0 info disc:0` runs; volume name + title table printed |
| Title table | Columns: #, Source, Duration, Size, Ch, Resolution, FPS, Audio |
| Title selection | Prompt; enter `2-5` or similar |
| Content type | `[1] TV episodes / [2] Movie`; enter `1` |
| Show name | Volume name shown as default; press Enter or edit |
| Season / first episode | Numbers; episode preview `→ titles will be S01E05, S01E06, …` |
| Confirm episodes | `Confirm? [Y/n]` — Enter |
| Staging dir | `Rip to /mnt/media/staging/<show>/ [Y/n]` — Enter |
| MakeMKV output | `makemkvcon mkv` streams to terminal |
| Probe | Video/Audio/Subtitle tables; Audio has Ch + Bitrate columns; Subtitles has Duration column |
| Audio selection | `Audio tracks (1-N, …) [1]:` |
| Transcode prompt | Fires only for TrueHD/DTS/PCM/FLAC/etc.; not for AC3/EAC3/AAC/MP3/Opus |
| Subtitle selection | `Subtitle tracks (…) [none]:` |
| CRF / Preset / Decomb | Defaults in brackets; decomb skipped for progressive sources |
| Preview | `S01E05  Show - S01E05.mkv  (a=[1:ac3@640,2:copy] s=[] crf=20 preset=slow)` |
| Queue | Enter → `Queued N job(s) (batch M). Worker will pick them up.` |

Verify:

```bash
rne ls    # jobs appear as queued
```

### 2. Movie ingest

- Select one title, choose `[2] Movie`, accept or edit title, queue.
- Output path: `/mnt/media/staging/<movie>/<movie>.mkv`

### 3. Rip failure handling

If a title fails, the prompt:

```
Title N failed. Abort the whole ingest, or skip and continue? [a/s]
```

- `a` → exits 1, no DB rows inserted
- `s` → continues with remaining titles

### 4. Edit escape hatch at preview

At `[Y/n/edit]:`, enter `edit`. JSON plan opens in `$EDITOR`. Change `quality` to 24,
save and quit. Preview re-displays with `crf=24`. Enter `y` to queue.

### 5. `rne ls`

```bash
rne ls               # queued + running + last-24h terminal
rne ls --all         # full history
rne ls --status done # filter
```

### 6. `rne edit <id>`

```bash
rne edit 1           # opens handbrake_args JSON in $EDITOR
rne edit <running>   # refuses with error message, exits 1
```

### 7. `rne cancel` / `rne retry`

```bash
rne cancel 2   # sets status=cancelled
rne ls         # verify
rne retry 2    # requeues, bumps attempt_count
rne ls         # verify
```

### 8. `rne pause` / `rne resume`

```bash
rne pause    # queue_settings.paused=1; worker idles after current job
rne resume   # queue_settings.paused=0
```

## What cannot be tested in this dev container

- Disc detection / ripping (`makemkvcon` not installed)
- Probe of ripped MKV (`ffprobe` not installed)
- HandBrake encoding (flatpak not present)

All interactive-flow logic, previews, and DB insertion are covered by unit tests
(`pytest tests/test_cli.py`).
