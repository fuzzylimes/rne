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
rsync -av dist/rne-0.2.1-py3-none-any.whl rip@rip:~/

# On VM: install and set up services
pipx install ~/rne-0.2.1-py3-none-any.whl
rne service install
loginctl enable-linger rip
systemctl --user enable --now rne-worker rne-dashboard
```

Dashboard: `http://localhost:8500/`

## Usage

### Ingest a disc

Put a disc in the drive, then:

```bash
rne ingest           # default minlength: 900s (filters short bonus clips)
rne ingest -m 1200   # stricter filter — useful for discs with long extras
rne ingest -m 0      # no filter — shows every title on the disc
```

The CLI walks through title detection, content classification (TV or movie), naming, audio/subtitle track selection, and encoding parameters. At the end it rips the selected titles and queues the encode jobs. The worker picks them up automatically.

The `--minlength` / `-m` value is passed to both the title-listing and ripping steps so that title indices are always consistent between the two.

Metadata can be pre-supplied on the command line to skip the corresponding prompts:

```bash
rne ingest -n "Initial D" -sn 1 -fe 5    # TV: show, season, first episode
rne ingest -n "Aliens"                   # name only — content type still prompted
```

- `-n` / `--name` — show or movie name.
- `-sn` / `--season` — season number (0 for specials). TV only.
- `-fe` / `--first-episode` — first episode number. TV only.

Providing `-sn` or `-fe` implies TV episodes, so the content-type prompt is skipped. Anything not provided is still prompted for. When all three are given, the only metadata prompt left is the `→ titles will be ...` confirmation.

Example session flow:

1. Title list from `makemkvcon -r --minlength=900 info disc:0` — sorted by `.mpls` source name so episodes appear in the correct order regardless of how the publisher arranged them on disc. The `#` column is the display index; `Disc Index` shows the underlying MakeMKV title number.
2. Select titles by display `#`: `0-7`, `0,2,4`, `all`, or empty to abort
3. TV or Movie? → if TV with exactly one title selected, asks about multi-episode disc mode first (see below), then prompts for show/season/starting episode; if movie, prompts for title
4. Confirm staging directory, then rip
5. Probe of first file — shows video/audio/subtitle track table
6. Audio tracks to encode, subtitle tracks, CRF quality, preset, detelecine (DVD + NTSC only), decomb. The preset default is `medium` for DVD sources (`mpeg2video` codec or `--dvd` flag) and `slow` for everything else (Blu-rays)
7. Preview of all queued jobs — confirm or edit before inserting

If a title fails to rip, it is retried automatically (once by default — see `RNE_RIP_RETRIES` under Configuration). Once automatic retries are exhausted you are asked whether to abort the whole ingest, retry the title again, or skip it and continue:

```
Title 5 failed. Abort the whole ingest, retry the title, or skip and continue? [a/r/s]
```

### Queue already-ripped files

For files ripped outside of `rne ingest` (e.g. re-queuing after a failed encode, or manually-ripped sources):

```bash
rne queue /path/to/file.mkv           # single file
rne queue /path/to/directory/         # all .mkv files in the directory, alphabetical order
rne queue --dvd /path/to/file.mkv     # treat source as DVD (forces detelecine prompt for NTSC frame rates)
```

The `--dvd` flag is only needed when the source codec isn't `mpeg2video` — for genuine DVD rips the flag is usually redundant, but it's there as an override.

When queuing a single TV file, the CLI also asks whether it is a multi-episode disc (see below).

Source files are never moved or copied. Do not move or delete them until encoding completes.

### Multi-episode discs

Some discs (common in anime releases) pack all episodes into a single title using chapters rather than separate titles. When you select exactly one TV title — in either `rne ingest` or `rne queue` — the CLI asks:

```
Is this a multi-episode disc file (split by chapters)? [y/N]
```

If yes, after the probe and encoding config prompts you'll see the chapter table and an episode-length prompt:

```
  Total chapters : 12
  Total duration : 1:52:30

Episode length (minutes) [24]:
```

The tool auto-detects episode boundaries by grouping chapters until their combined duration is close to the target. Short chapters (OP/ED sequences, previews) are absorbed naturally into adjacent episodes. It then shows the proposed split and lets you adjust before committing:

```
  Ep      Chapters  Duration
   1           1-2    24:15
   2           3-4    23:45
   ...

  [a] Accept
  [r] Re-split with a different episode length
  [f] Re-split with fixed chapters per episode
  [m] Manually enter chapter ranges
  [q] Quit
```

Each accepted episode becomes a separate encode job in the queue. All jobs share the same source file; HandBrake's `--chapters` flag handles the splitting at encode time. No temporary files are created.

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
| `DEFAULT_PRESET` | `slow` | Preset default for Blu-ray (non-DVD) sources |
| `DEFAULT_PRESET_DVD` | `medium` | Preset default for DVD sources |
| `RIP_RETRIES` | `1` | Automatic retries when a title rip fails; override with `RNE_RIP_RETRIES` env var (clamped to 0–10) |

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
uv build            # build wheel → dist/rne-0.2.1-py3-none-any.whl
```

Tests use in-memory SQLite; no external binaries required.
