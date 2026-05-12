# rne — Rip-and-Encode Pipeline

A personal media pipeline for Blu-ray and DVD: disc → rip (MakeMKV) → encode (HandBrake) → done. Combines ripping, probing, and encoding into a unified queue with a background worker and a web dashboard, so you can keep inserting discs while the encoder catches up over hours or days.

## How it works

Three processes coordinate through a single SQLite database:

- **`rne ingest`** — interactive command you run with a disc in the drive. Detects titles, prompts for selections and encoding parameters, rips, probes, and queues the jobs. Done in minutes.
- **`rne-worker`** — long-running daemon under systemd. Claims queued jobs, runs HandBrakeCLI, writes progress and results back to the DB. One job at a time.
- **`rne-dashboard`** — Flask web UI at `http://localhost:8500/`. Shows the queue, live encode progress, and recent history. Pause/resume/retry from the browser.

## Quick install (on the rip VM)

See [docs/install.md](docs/install.md) for the full prerequisite checklist and step-by-step runbook. The short version:

```bash
# Build on Mac, copy wheel to VM
uv build
rsync -av dist/rne-0.1.0-py3-none-any.whl rip@rip:~/

# On VM: install and set up services
pipx install ~/rne-0.1.0-py3-none-any.whl
rne service install
loginctl enable-linger rip
systemctl --user enable --now rne-worker rne-dashboard
```

Dashboard: `http://localhost:8500/`

## Usage

### Ingest a disc

Put a disc in the drive, then:

```bash
rne ingest
```

The CLI walks through title detection, content classification (TV or movie), naming, audio/subtitle track selection, and encoding parameters. At the end it rips the selected titles and queues the encode jobs. The worker picks them up automatically.

Example session flow:

1. Title list from `makemkvcon info disc:0`
2. Select titles: `0-7`, `0,2,4`, `all`, or empty to abort
3. TV or Movie? → prompts for show/season/episode (or movie title)
4. Confirm staging directory, then rip
5. Probe of first file — shows video/audio/subtitle track table
6. Audio tracks to encode, subtitle tracks, CRF quality, preset, decomb
7. Preview of all queued jobs — confirm or edit before inserting

### Check queue status

```bash
rne ls          # queued, running, recent terminal states
rne ls --all    # full history
```

### Manage jobs

```bash
rne pause              # pause the global queue (current encode continues)
rne resume             # resume

rne cancel <id>        # remove a queued job (terminal, CLI only)
rne retry <id>         # re-queue any terminal-state job
rne edit <id>          # edit handbrake_args JSON in $EDITOR
```

Pause/resume/retry are also available as buttons on the dashboard.

### Probe a file

```bash
rne probe <file>           # video/audio/subtitle track summary
rne probe --deep <file>    # full packet scan (slow on large Blu-rays)
```

## Configuration

Defaults live in `src/rne/config.py`. Key constants:

| Constant | Default | Notes |
|---|---|---|
| `STAGING_ROOT` | `/mnt/media/staging` | Override with `RNE_STAGING_ROOT` env var |
| `RNE_DB` | `~/.local/state/rne/jobs.db` | Override with `RNE_DB` env var |
| `COPY_FRIENDLY_AUDIO_CODECS` | `ac3, eac3, aac, mp3, opus` | Tracks with these codecs are copied; others trigger a transcode prompt |
| `AC3_BITRATE_BY_CHANNELS` | 96/192/640 kbps | Recommended AC3 bitrate by channel count |

## Output layout

```
/mnt/media/staging/Initial D/
    _raw/
        batch-1/
            title_t00.mkv            ← raw from MakeMKV
            title_t01.mkv
    Season 01/
        Initial D - S01E05.mkv       ← encoded by worker

/mnt/media/staging/The Silence of the Lambs/
    _raw/
        batch-2/
            title_t00.mkv            ← raw
    The Silence of the Lambs.mkv    ← encoded
```

Raw files are kept in `_raw/batch-{id}/` under the show/movie staging directory, using MakeMKV's `title_tNN.mkv` names. After verifying the encode, move files to your library manually — rne does not manage the library.

## Reliability

- **VM reboot mid-encode** — worker reconciles any `running` row to `interrupted` on restart; the `.partial` output is preserved; dashboard shows a Retry button.
- **Worker crash** — systemd `Restart=on-failure` brings it back within 5 seconds.
- **`/mnt/media` not mounted** — `ConditionPathExists` holds both services until the mount is ready.
- **Partial outputs** — HandBrake writes to `{output}.partial`; atomic rename on success. A failed or interrupted encode never leaves a file that looks complete.

## Development

```bash
uv sync             # install deps including dev group
uv run pytest       # run tests
uv run ruff check   # lint
uv build            # build wheel → dist/rne-0.1.0-py3-none-any.whl
```

Tests use in-memory SQLite; no external binaries required.
