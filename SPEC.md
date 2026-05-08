# rne — Rip-and-Encode Pipeline

A personal media pipeline that takes a Blu-ray or DVD from disc to encoded MKV with minimal user error and no babysitting. Combines ripping (MakeMKV), probing (ffprobe), and encoding (HandBrake) into a unified queue with a worker daemon and a lightweight dashboard.

## Background

The author currently runs a manual three-step process on a dedicated VM:

1. Identify and select titles from a disc with `mkvrip` (a wrapper around `makemkvcon info`).
2. Rip selected titles to staging.
3. Manually inspect each ripped MKV with `mkvprobe-format.py` (an `ffprobe` wrapper), then construct and run a HandBrake command for each file. The `batchbrake` tool generates these HandBrake commands in batch, but is fundamentally a script generator — the user still runs the script and waits.

This worked acceptably for DVDs, where rip and encode times were roughly 1:1. With the move to Blu-rays and an `x265 --preset slow` encode profile, encoding now takes 10–20× longer than ripping. This causes a backlog: ripped files sit waiting for encode, the user forgets which titles came from which disc, and the synchronous nature of the workflow breaks down.

`rne` replaces this workflow with a queue-based pipeline: ingest collects everything needed in a single interactive pass, jobs go on a queue, a worker daemon encodes them sequentially in the background, and a dashboard shows status. The user can keep ripping discs while encodes catch up over hours or days.

## Goals

- Reduce user error during disc ingestion by asking each question once with a sensible default.
- Decouple ripping from encoding so the user can ingest discs faster than the encoder can process them.
- Make queue state observable (web dashboard) and manageable (CLI).
- Survive VM reboots without losing the queue or producing silent half-files.
- Stay simple. This is a personal project, not a product.

## Non-goals

- Multi-user support, authentication, or remote access beyond the LAN.
- Parallel encoding. One worker, one job at a time.
- Generic media management (library organization, metadata scraping, etc.). The output goes to a staging directory; the user moves it to the real library themselves.
- Mid-encode cancellation. The worker either runs a job to completion or is killed by `systemctl stop`, which marks the job interrupted on the next start.

## Environment

| Item | Value |
|---|---|
| Host | `rip` VM on the home Proxmox cluster |
| OS | Ubuntu 24.04 |
| User | `rip` (member of `mediagroup`, GID 1500) |
| Python | 3.12+ |
| Media root | `/mnt/media` (9p mount from Proxmox host) |
| Staging root | `/mnt/media/staging` |
| HandBrake | Flatpak: `fr.handbrake.ghb` |
| MakeMKV | `makemkv-bin` (PPA) |
| ffmpeg / ffprobe | apt |
| SQLite | 3.45+ (Ubuntu 24.04 default) |

## Architecture overview

Three independent processes coordinate through a single SQLite database:

- **Ingest CLI** (`rne`) — interactive command the user runs after putting a disc in the drive. Lists titles, prompts for selections and encoding parameters, rips the selected titles, probes the rips, and inserts jobs into the queue. Exits when done.
- **Worker daemon** (`rne-worker`) — long-running process under systemd. Pulls the next queued job, runs HandBrakeCLI, writes progress and completion state back to the DB. Single-threaded with respect to encoding.
- **Dashboard** (`rne-dashboard`) — Flask web app under systemd. Renders the queue as HTML, exposes pause/resume/retry buttons. Auto-refreshes every 30 seconds.

The processes do not talk directly. All coordination is via SQLite. They all read and write `/mnt/media`, but the filesystem is data, not coordination.

### Why SQLite, not files-as-queue

A "directory of job files" was considered and rejected. SQLite handles state transitions atomically, supports the dashboard's "what's queued vs running vs failed in the last 24h" queries trivially, and gives us cancellation flags and progress updates without needing a second mechanism. It's a single file with no daemon to run.

WAL mode is mandatory — without it, the worker's writes would block dashboard reads. Connection setup (run on every connect):

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA synchronous  = NORMAL;
```

## Data model

### Job state machine

Jobs flow through these states:

```
queued → running → done
              └── → failed
              └── → interrupted   (worker killed mid-encode)

queued ↔ paused                   (toggle from dashboard)
queued → cancelled                (CLI only, terminal)
{any terminal state} → queued     (retry; resets row, bumps attempt_count)
```

Notes on transitions:

- `running → cancelled` does not exist. To stop a running job, `systemctl stop rne-worker`. The worker's SIGTERM handler forwards to the HandBrake child; on next start, `running` rows with no live worker are reconciled to `interrupted`.
- `paused` is for queued jobs the user wants to keep but skip. The worker never claims paused jobs.
- `cancelled` is terminal removal from the queue, available via CLI only. The dashboard does not expose cancel.
- Retry resets the row to `queued`, increments `attempt_count`, and clears `progress_pct`, `progress_fps`, `progress_eta`, `error_message`, `exit_code`, `started_at`, and `finished_at`. Clearing `started_at` and `finished_at` is required: the dashboard's "Recent (last 24h)" query filters on `finished_at`, so a retried job with a stale `finished_at` would reappear in that section until it actually finishes. Retry works from any terminal state including `done` (the user may have realized they picked the wrong audio track). When a `done` job is retried, the worker produces a new `{output_path}.partial` that is atomically renamed over the existing output file — this is intentional: it lets the user correct a wrong audio track selection without manual cleanup.

### Schema

```sql
-- One row per logical job. On retry, the same row is reset; not a new row.
CREATE TABLE jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Logical identity. Either (show, season, episode) is set, OR movie is set.
    show              TEXT,
    season            INTEGER,
    episode           INTEGER,
    movie             TEXT,

    -- File paths
    source_path       TEXT NOT NULL,    -- ripped MKV from the ingest stage
    output_path       TEXT NOT NULL,    -- worker writes {output_path}.partial first

    -- HandBrake configuration as JSON. Worker re-renders the command from this each attempt.
    handbrake_args    TEXT NOT NULL,

    -- Lifecycle
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','paused','running',
                                        'done','failed','cancelled','interrupted')),
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    priority          INTEGER NOT NULL DEFAULT 0,    -- lower runs sooner

    -- Progress (worker writes every ~20s while running; NULL otherwise)
    progress_pct      REAL,
    progress_fps      REAL,
    progress_eta      INTEGER,                       -- seconds remaining

    -- Timing
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,

    -- Diagnostics
    exit_code         INTEGER,
    error_message     TEXT,                          -- short summary; full stderr in journal

    -- Grouping
    ingest_batch_id   INTEGER REFERENCES ingest_batches(id),

    CHECK ((show IS NOT NULL AND movie IS NULL) OR (show IS NULL AND movie IS NOT NULL))
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_claim  ON jobs(priority, id) WHERE status = 'queued';
CREATE INDEX idx_jobs_batch  ON jobs(ingest_batch_id);

-- Groups jobs from a single disc ingest. Lets the dashboard show
-- "Initial D Disc 3 — 7 episodes, 5 done, 1 running, 1 queued" as a unit.
CREATE TABLE ingest_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,                       -- e.g. "Initial D S01 Disc 3"
    show        TEXT,
    movie       TEXT,
    notes       TEXT,                                -- BD volume name, etc.
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Single-row table. Worker writes here every ~10s while alive.
CREATE TABLE worker_status (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    pid             INTEGER,
    state           TEXT NOT NULL DEFAULT 'starting'
                    CHECK (state IN ('starting','idle','encoding','stopping')),
    current_job_id  INTEGER REFERENCES jobs(id),
    last_seen       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO worker_status (id) VALUES (1);

-- Single-row table. Global queue pause flag.
CREATE TABLE queue_settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    paused  INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1))
);
INSERT INTO queue_settings (id) VALUES (1);
```

### handbrake_args JSON shape

Stored as JSON, not a pre-rendered shell command. The worker re-renders the HandBrake command from this JSON on every claim, sharing rendering code with the ingest CLI. Adding new fields later doesn't require migrating old rows.

```json
{
  "encoder": "x265",
  "quality": 20,
  "preset": "slow",
  "audio_tracks": [
    {"track": 1, "codec": "ac3",  "bitrate": 640},
    {"track": 2, "codec": "copy"}
  ],
  "subtitle_tracks": [],
  "decomb": false,
  "extra_args": []
}
```

`audio_tracks` is a list of per-track objects, not a list of indexes. Each entry carries its own copy/transcode decision so a single ingest can mix copied and transcoded tracks (the common Blu-ray case: copy the AC3 stereo track, transcode the TrueHD 5.1 to AC3 5.1).

`AudioTrack` fields:
- `track` — 1-based stream index from the source file. Required.
- `codec` — `"copy"` to mux as-is, or a HandBrake encoder name (`"ac3"`, `"eac3"`, `"aac"`, etc.). Defaults to `"copy"` if omitted.
- `bitrate` — integer kbps, required when `codec` is anything other than `"copy"`. Rejected (validation error) when `codec == "copy"`. No default; the ingest CLI fills this in based on channel count when it builds the args, but `handbrake.py` does not infer it.

`extra_args` is the escape hatch for one-off flags without changing schema.

### Audio codec policy

Track selection is the user's decision. Whether a selected track gets copied or transcoded is determined by its source codec:

**Copy-friendly codecs** — copy by default, no prompt. These play universally on Jellyfin clients (TVs, phones, browsers):

```python
COPY_FRIENDLY_AUDIO_CODECS = frozenset({"ac3", "eac3", "aac", "mp3", "opus"})
```

**Anything else triggers a transcode prompt** during ingest. This includes TrueHD, DTS, DTS-HD MA, DTS-HD HRA, PCM, FLAC. The recommendation is always AC3 at a channel-appropriate bitrate, since AC3 has the broadest playback support:

| Source channels | Recommended AC3 bitrate |
|---|---|
| 1.0 (mono) | 96 kbps |
| 2.0 (stereo) | 192 kbps |
| 5.1 | 640 kbps |
| 7.1 | 640 kbps (AC3 max) |

Both sets live in `config.py` as tunable constants. To treat DTS as copy-friendly, add `"dts"` to the set; the ingest CLI re-reads the constant on every run.

The prompt itself only appears when the source codec is *not* in `COPY_FRIENDLY_AUDIO_CODECS`. For DVDs (where AC3 is universal), the user never sees the prompt. For Blu-rays with a TrueHD or DTS-HD track, the prompt appears once per non-friendly track.

## Project layout

```
rne/
├── pyproject.toml
├── README.md
└── src/rne/
    ├── __init__.py
    ├── __main__.py          # python -m rne dispatch
    ├── config.py            # paths, env defaults, codec policy
    ├── db.py                # connect(), pragmas, schema init, common queries
    ├── models.py            # Job, AudioTrack, HandbrakeArgs, JobStatus
    ├── handbrake.py         # args JSON → command list (pure function)
    ├── probe.py             # ffprobe wrapper, stream summary
    ├── makemkv.py           # makemkvcon wrapper
    ├── systemd/
    │   ├── __init__.py
    │   ├── rne-worker.service
    │   └── rne-dashboard.service
    ├── cli/
    │   ├── __init__.py      # argparse dispatcher
    │   ├── ingest.py        # the disc-to-queue interactive flow
    │   ├── ls.py
    │   ├── edit.py
    │   ├── manage.py        # cancel, retry, pause, resume
    │   ├── service.py       # rne service install / uninstall
    │   └── prompts.py       # shared input helpers
    ├── worker/
    │   ├── __init__.py
    │   ├── daemon.py        # main loop
    │   ├── runner.py        # subprocess.Popen of HandBrakeCLI
    │   └── heartbeat.py     # periodic worker_status update
    └── dashboard/
        ├── __init__.py
        ├── app.py           # Flask factory + main()
        ├── routes.py        # GET /, POST /jobs/<id>/{pause,resume,retry}
        └── templates/
            ├── base.html
            └── index.html
```

### Module ownership

- **`config.py`** — flat module of constants. Things like `DB_PATH`, `STAGING_ROOT`, `MEDIA_ROOT`, default minlength, default ffprobe timeout, the flatpak HandBrake invocation prefix, `COPY_FRIENDLY_AUDIO_CODECS`, `AC3_BITRATE_BY_CHANNELS`. Override via env vars where appropriate. Not pydantic, not yaml. When the project grows past 10 settings, revisit.
- **`db.py`** — owns everything sqlite. Schema as a `SCHEMA_SQL` constant, `init_db()` that runs it idempotently, `connect()` that applies pragmas. Thin functions like `claim_next_job()`, `update_progress(...)`, `mark_done(...)`. No ORM. `sqlite3.Row` row factory. One-off queries can stay inline at the call site.
- **`models.py`** — dataclasses mirroring the columns, with `from_row(row)` classmethods. `JobStatus` is a `StrEnum`. `HandbrakeArgs` and the nested `AudioTrack` dataclass with `to_json()` / `from_json()`.
- **`handbrake.py`** — pure. Takes a `HandbrakeArgs` and source path, returns `["HandBrakeCLI", "-i", ...]`. No subprocess, no DB, no I/O. The flatpak prefix from `config.py` is prepended here. Validates `AudioTrack` entries: bitrate required when codec != "copy", rejected when codec == "copy".
- **`probe.py`** — port of `mkvprobe-format.py`. `summarize(mkv_path) → StreamSummary`. The `AudioStream` summary includes `codec`, `channels`, `bitrate`, `language`, `title`, `default`, `forced`. The table-printing logic moves into `cli/ingest.py`.
- **`makemkv.py`** — port of `mkvrip`'s parser (the `parse_info`, `summarize`, `parse_index_spec` functions). The interactive prompting moves into `cli/ingest.py`.
- **`worker/runner.py`** — `subprocess.Popen` of HandBrake, parses progress, captures stderr ring buffer, handles `.partial` rename.
- **`worker/daemon.py`** — main loop. Reconcile orphans → loop forever: check pause, claim, run, repeat.
- **`worker/heartbeat.py`** — daemon thread. Updates `worker_status.last_seen` every 10s.
- **`dashboard/app.py`** — Flask factory and `main()` (binds `0.0.0.0:8500`).
- **`dashboard/routes.py`** — six handlers, each ~5 lines. POST handlers return `redirect('/', 303)` (PRG pattern).

### pyproject.toml

```toml
[project]
name = "rne"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["flask>=3.0"]

[project.scripts]
rne = "rne.cli:main"
rne-worker = "rne.worker.daemon:main"
rne-dashboard = "rne.dashboard.app:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### Dependencies

- **Runtime**: `flask` only.
- **Dev**: `pytest`, `ruff`.
- **Stdlib covers**: `sqlite3`, `subprocess`, `argparse`, `dataclasses`, `pathlib`, `signal`, `threading`, `re`, `json`.
- **External binaries**: `makemkvcon`, `HandBrakeCLI` (via flatpak), `ffprobe`. Optional: `mkvmerge` from `mkvtoolnix-cli` as a fallback for subtitle metadata if `ffprobe` proves flaky on some discs.

The whole thing fits in a venv with maybe 3 MB of installed packages.

## Storage layout

Raw files are isolated per ingest batch to prevent cross-batch clobbering when disc 2 is ingested while disc 1's encodes are still in the queue:

```
/mnt/media/staging/Initial D/
    _raw/
        batch-7/
            B1_t00.mkv                         ← raw from makemkv (disc 1, real name varies)
            A1_t01.mkv
            D1_t02.mkv
            C1_t03.mkv
        batch-9/
            00038.mkv                          ← raw from makemkv (disc 2, real name varies)
            00039.mkv
    Season 01/
        Initial D - S01E05.mkv                 ← encoded
        Initial D - S01E06.mkv
        ...

/mnt/media/staging/The Silence of the Lambs/
    _raw/
        batch-12/
            D1_00.mkv                          ← raw (real name varies)
    The Silence of the Lambs.mkv               ← encoded
```

Raw filenames are whatever makemkv produces from the disc's TINFO fields — they can be anything (`B1_t00.mkv`, `00038.mkv`, `D1_00.mkv`, etc.). **rne never constructs or predicts raw filenames.** The only assumption is that exactly one new `*.mkv` file appears in the batch raw dir per rip; the ingest CLI snapshots the dir before and after each rip to detect the new file.

Raw files live inside a `_raw/batch-{ingest_batch_id}/` subdir. Encoded files for TV go in a `Season NN/` subdir alongside `_raw/`; for movies they sit at the show root alongside `_raw/`. After verifying, the user moves the encoded files to the real library themselves; this tool does not manage the library.

Output filename templates:

- TV: `{staging_root}/{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d}.mkv`
- Movie: `{staging_root}/{movie}/{movie}.mkv`

## Ingest CLI flow (`rne ingest`)

The user runs this once per disc.

### Step 1 — Disc detection

Run `makemkvcon info disc:0`. On failure (no disc, drive busy), print stderr verbatim and exit non-zero. On success, print the disc volume name and the title table — same format as the existing `mkvrip` script's output.

### Step 2 — Title selection

Same prompt as the existing script:

```
Titles to rip (e.g. '0-7', '0,2,4', 'all', empty to abort):
```

Same `parse_index_spec` logic, ported from `mkvrip` to `cli/prompts.py`. Empty input aborts.

### Step 3 — Content classification and naming

Asked once per ingest:

```
What's on this disc?
  [1] TV episodes
  [2] Movie
> 1
```

If TV: prompt for show, season, and starting episode number. The disc volume name from `makemkvcon info` (CINFO field 2) is offered as the default for the show name — it's frequently garbage like `INITIAL_D_S1_D1` but sometimes useful as a hint. The user edits or accepts.

The remaining episodes auto-increment in disc order:

```
Show [INITIAL_D_S1_D1]: Initial D
Season: 1
First episode number: 5
  → titles will be S01E05, S01E06, S01E07, S01E08
Confirm? [Y/n]
```

The confirmation matters: "starting episode wrong" is the most common ingest mistake. Catching it here is much cheaper than discovering it after 8 hours of encoding.

If Movie: prompt for the title, same default-from-disc behavior. One job, no episode numbers.

### Step 4 — Staging dir confirm and rip

At the start of step 4, the CLI performs these operations in order:

1. **INSERT into `ingest_batches`**, capture `cursor.lastrowid` as `batch_id`.
2. **Construct `raw_dir`** = `{staging_root}/{show}/_raw/batch-{batch_id}/`.
3. **`raw_dir.mkdir(parents=True, exist_ok=False)`** — `exist_ok=False` is intentional: a fresh batch id must produce a fresh dir; if the dir already exists something is wrong (id collision or stale state) and a clear error is preferable to silently mixing rips.
4. **Confirm and rip** (see below).

Right before the rip kicks off:

```
Rip to /mnt/media/staging/Initial D/_raw/batch-7/ [Y/n]:
```

`n` opens a path prompt to override the staging root. The batch-scoped `_raw/batch-{id}/` suffix is always appended. This handles "disc title was hot garbage and I forgot to fix it in step 3" without forcing every ingest through an extra prompt.

Then, for each selected title **in disc-selection order**:

a. Snapshot `before = set(raw_dir.glob("*.mkv"))`.
b. Run `makemkvcon mkv` for this title. stdout streams to the terminal so the user sees progress bars.
c. Snapshot `after = set(raw_dir.glob("*.mkv"))`. Compute `new = after - before`. If `len(new) != 1`, abort with a clear error showing what is in the dir.
d. Append `(title_idx, new_file_path)` to the **rip manifest** — an in-memory ordered list of `(title_idx, Path)` pairs that maps disc-title order to actual filenames.

If a rip fails (non-zero exit), prompt:

```
Title 5 failed. Abort the whole ingest, or skip and continue? [a/s]
```

Never silently queue a partial batch.

The rip manifest is the authoritative record of which file belongs to which title. Raw filenames are whatever makemkv produces — the only assumption rne makes is that exactly one new `*.mkv` file appears per rip.

### Step 5 — Probe the first ripped file

Run `ffprobe` on `rip_manifest[0].path` (the first file from the rip manifest), print the video/audio/subtitle table. Extends the existing `mkvprobe-format.py` output:

- **Video**: as today (codec, resolution, fps, field order, lang, default, forced).
- **Audio**: add a **Bitrate** column and a **Channels** column. Bitrate is read from `stream.bit_rate` (always present for AC3/DTS, sometimes missing for TrueHD where it's container-level). If `bit_rate` is absent for a stream, the probe table shows `—` for that track; no arithmetic fallback is performed. Channels from `stream.channels`. Bitrate catches the common Blu-ray case where two tracks are both labeled AC3 but one is 10× the other; channels feeds the transcode-bitrate recommendation in step 6.
- **Subtitles**: add a **Duration** column. ffprobe reports stream duration from the header for free. Full subtitle tracks span the movie's duration; forced subtitle tracks span much less (sum of forced display times). This is a free, reliable signal for forced-vs-full classification — the `forced` disposition flag from MakeMKV has been observed to be unreliable.

**Deep packet scan is opt-in only.** The full `-count_packets` scan exists as a separate `rne probe --deep <file>` command. It is not run automatically — observed to take >5 minutes on a 30 GB Blu-ray, which is unacceptable for the ingest path. The 60s timeout applies to the standard `rne probe` path only; `rne probe --deep` has no built-in timeout (the user invoked it knowing it is slow and can Ctrl-C).

If subsequent files in the batch differ in track layout, warn but continue — most discs are uniform.

### Step 6 — Encoding configuration

Asked once per ingest. Defaults in brackets; empty input accepts.

```
Audio tracks (1-3, comma-separated, 'all') [1]:
Subtitle tracks (1-2, comma-separated, 'none') [none]:
Quality (CRF) [20]:
Preset [slow]:
Decomb? Source is 1080i. [y/N]: y
```

After the audio-track *selection* prompt, the CLI inspects each chosen track's codec and channels (from the probe data already in hand). For each selected track:

- **If codec is in `COPY_FRIENDLY_AUDIO_CODECS`**: silently mark as `{"track": N, "codec": "copy"}`. No prompt.
- **If codec is not copy-friendly**: prompt for the transcode decision.

Per-track transcode prompt:

```
Track 1 is TrueHD 5.1. Transcode? [Y/n/c]
  Y - transcode to AC3 5.1 @ 640k (recommended)
  n - copy as-is (lossless, large file)
  c - custom codec/bitrate
```

Where the recommended bitrate comes from `AC3_BITRATE_BY_CHANNELS[track.channels]`. Default is `Y` (transcode to AC3) — universally playable on Jellyfin clients, much smaller file. `n` keeps the lossless track. `c` opens two follow-up prompts for codec name and bitrate, no validation beyond "bitrate must be a positive integer".

Examples:
- DVD with one AC3 track selected: zero prompts (codec is copy-friendly).
- Blu-ray with AC3 2.0 + AC3 5.1 selected: zero prompts (both copy-friendly).
- Blu-ray with TrueHD 5.1 + AC3 2.0 selected: one prompt (track 1 is TrueHD), default Y produces `[{"track": 1, "codec": "ac3", "bitrate": 640}, {"track": 2, "codec": "copy"}]`.
- Blu-ray with TrueHD 2.0 + DTS 5.1 selected: two prompts, one per non-friendly track.

Other notes:
- **Audio codec is always copy when possible.** Selection alone is the decision for copy-friendly tracks.
- **For MKV output, `--audio-fallback` is meaningless.** MKV holds any codec. Track-level codec is the real decision.
- **Decomb prompt only appears for interlaced sources.** For progressive Blu-ray it's skipped entirely. For DVDs it'll usually show up.
- All defaults come from `config.py`. Tune once, forget.

### Step 7 — Output preview, mismatch detection, edit, confirm

Before showing the preview, probe each remaining file in the rip manifest (titles 2..N) and compare its track layout to title 1's using `probe.layouts_match()`. The comparison checks:

- same audio track count and codecs at the same indices
- same subtitle track count

If all layouts match, the preview and confirm prompt are identical to the all-matching case below.

If any titles diverge, their preview line is annotated with `⚠ different track layout` and a per-title description appears below the preview:

```
Preview:
  S01E05  Initial D - S01E05.mkv  (a=[1:ac3@640,2:copy] s=[] crf=20 preset=slow)
  S01E06  Initial D - S01E06.mkv  (a=[1:ac3@640,2:copy] s=[] crf=20 preset=slow)
  S01E07  Initial D - S01E07.mkv  (a=[1:ac3@640,2:copy] s=[] crf=20 preset=slow)  ⚠ different track layout
  S01E08  Initial D - S01E08.mkv  (a=[1:ac3@640,2:copy] s=[] crf=20 preset=slow)

S01E07 differs: audio track 2 is dts instead of ac3.

Queue these 4 jobs? [Y/n/edit/skip-mismatched]:
```

- **Y** (default) — queue all; apply the same encoding config to mismatched titles.
- **n** — abort without queuing anything.
- **edit** — open the full queue plan as JSON in `$EDITOR`, re-validate, re-show the preview. The mismatch warnings persist; they reflect track layout, not encoding config.
- **skip-mismatched** — queue only the titles whose layout matches title 1. Orphan raws for skipped titles are left on disk; the user can manually process them later (`rne ingest --add` is a planned future feature, not implemented yet).

When all layouts match, the confirm prompt is the original three-way `[Y/n/edit]`.

The audio summary format `[1:ac3@640,2:copy]` shows track index, action, and bitrate (when transcoding) so the user can verify at a glance.

### Step 8 — Insert and exit

Insert N `jobs` rows in `queued` state (the `ingest_batches` row was already created at the start of step 4). The `source_path` on each row is the actual file path from the rip manifest — never a constructed `title_tNN.mkv` path. Print:

```
Queued 4 jobs (batch 17). Worker will pick them up. Run `rne ls` to check status.
```

CLI exits. The worker (running independently under systemd) claims the first job within seconds.

## Other CLI subcommands

- **`rne ls`** — list jobs with status, optionally filtered. `--all` shows full history. Default shows queued + running + recent terminal states.
- **`rne edit <id>`** — opens that job's `handbrake_args` JSON in `$EDITOR`. On save, validates and writes back. **Refuses to edit a `running` job** with a non-zero exit. Editing other states (queued, paused, failed, interrupted, cancelled, done) is allowed. Validation criteria (all must pass; on failure the user is offered a chance to re-open the editor):
  1. **JSON parses** — the file content is valid JSON.
  2. **Conforms to `HandbrakeArgs` schema** — the top-level object contains only the known fields (`encoder`, `quality`, `preset`, `audio_tracks`, `subtitle_tracks`, `decomb`, `extra_args`); unknown keys are rejected.
  3. **`AudioTrack` invariant** — for each entry in `audio_tracks`: `bitrate` is required (positive integer) when `codec != "copy"`, and must be absent when `codec == "copy"`.
  4. **TOCTOU re-check** — after the editor closes and validation passes, re-query the job's status. If it has transitioned to `running` while the editor was open, refuse the save with the message `"job is now running; cannot edit"` and exit non-zero. This guards against the race where the worker claims a queued job while the user is editing it.
- **`rne cancel <id>`** — terminal removal of a queued job. Sets status to `cancelled`. CLI only — not exposed in the dashboard.
- **`rne retry <id>`** — resets any terminal-state job back to `queued`. Bumps `attempt_count`, clears progress and error fields.
- **`rne pause`** / **`rne resume`** — toggle the global `queue_settings.paused` flag. The worker checks this between jobs.

## Worker design

### Main loop (`worker/daemon.py`)

```python
def main():
    setup_signal_handlers()
    db.init()
    reconcile_orphans()
    start_heartbeat_thread()

    while not shutdown_requested():
        if queue_paused():
            sleep(5); continue

        job = db.claim_next_job()
        if job is None:
            sleep(5); continue

        run_job(job)
```

Five branches, no state held between iterations. Every loop pass is independent. If the worker dies and restarts at any point, picking up from where it left off is the same code path as cold start.

### Job execution (`worker/runner.py`)

```python
def run_job(job):
    # Clean up any leftover .partial from a previous attempt
    pathlib.Path(job.output_path + ".partial").unlink(missing_ok=True)

    cmd = handbrake.build_command(job.source_path,
                                  job.output_path + ".partial",
                                  job.handbrake_args)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 text=True, bufsize=1)
    _current_proc.set(proc)  # for SIGTERM forwarding

    last_progress_update = 0
    stderr_buf = collections.deque(maxlen=200)

    stderr_thread = threading.Thread(target=drain_to_deque,
                                     args=(proc.stderr, stderr_buf), daemon=True)
    stderr_thread.start()

    for line in proc.stdout:
        progress = parse_handbrake_progress(line)
        if progress and time.monotonic() - last_progress_update > 20:
            db.update_progress(job.id, **progress)
            last_progress_update = time.monotonic()

    proc.wait()
    stderr_thread.join(timeout=2)

    if proc.returncode == 0:
        os.rename(job.output_path + ".partial", job.output_path)
        db.mark_done(job.id)
    else:
        db.mark_failed(job.id, proc.returncode,
                       error_message="\n".join(stderr_buf)[-500:])
```

Decisions baked in:

- **stdout vs stderr split.** HandBrake prints progress to stdout, errors to stderr. Reading stdout in the main thread is fine (frequent lines). Stderr could go quiet for hours during a clean encode and dump 50 lines on failure — needs its own thread to avoid pipe-buffer deadlock.
- **Progress throttle.** 20-second wallclock minimum between DB writes, regardless of HandBrake's emission rate (multiple per second). Independent of system clock changes via `time.monotonic()`.
- **HandBrake progress format** is fixed: `Encoding: task 1 of 1, 12.34 % (45.6 fps, avg 50.0 fps, ETA 00h05m12s)`. One regex extracts pct, fps, ETA. ETA conversion is one line of arithmetic.
- **stderr ring buffer.** `deque(maxlen=200)` keeps the last 200 lines. On failure, the last 500 chars go into `error_message`. Full stderr goes to the systemd journal via `print(line, file=sys.stderr)` inside the drain function.
- **`.partial` rename.** Output goes to `{output_path}.partial` during encode, atomic rename on exit code 0. If the worker dies mid-encode, the `.partial` stays distinguishable from a complete output. A retry deletes any existing `.partial` first.

### Logging

One central log via the systemd journal. No per-job log files. `journalctl -u rne-worker -f` for live tail, `journalctl -u rne-worker --since="2 hours ago"` to find a specific failure. Built-in rotation, searchable, indexed by timestamp. The job row's `error_message` field holds a 500-char summary; the journal has the full stream if needed.

### Heartbeat

Daemon thread. Every 10 seconds, write `last_seen = CURRENT_TIMESTAMP` plus current state (`idle`, `encoding`, `stopping`) and `current_job_id` to the singleton `worker_status` row. Daemon thread so it dies with the main process. Dashboard reads this to render the "worker last seen Xs ago" indicator.

### Signal handling

```python
_shutdown = threading.Event()
_current_proc: contextvars.ContextVar[subprocess.Popen | None] = (
    contextvars.ContextVar("current_proc", default=None)
)

def setup_signal_handlers():
    def handle(signum, _frame):
        _shutdown.set()
        proc = _current_proc.get()
        if proc and proc.poll() is None:
            proc.terminate()
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
```

`systemctl stop rne-worker` sends SIGTERM. The worker forwards it to the active HandBrake child, the child exits, `run_job` sees a non-zero return and either marks the job failed or — if we know shutdown was requested — leaves it for the next-startup orphan reconciliation to mark interrupted (the orphan reconcile pattern naturally handles this: any `running` row at startup → `interrupted`).

`TimeoutStopSec=30` in the systemd unit gives HandBrake 30 seconds to wind down before systemd escalates to SIGKILL. That's plenty for a clean exit.

## Dashboard design

### Stack

- **Flask**, server-rendered Jinja templates, no JS framework, no fetch, no JSON API.
- **Pico CSS** via CDN — classless, ~10 KB, responsive, dark mode automatic.
- **Auto-refresh** via `<meta http-equiv="refresh" content="30">`. The page reloads every 30 seconds, picks up new state, re-renders. Crude and perfect for a single-user LAN dashboard.

### Endpoints

| Method | Path | Action |
|---|---|---|
| GET | `/` | render queue |
| POST | `/jobs/<id>/pause` | `status: queued → paused` |
| POST | `/jobs/<id>/resume` | `status: paused → queued` |
| POST | `/jobs/<id>/retry` | `status: any terminal → queued` (reset attempt fields) |
| POST | `/queue/pause` | set `queue_settings.paused = 1` |
| POST | `/queue/resume` | set `queue_settings.paused = 0` |

Every POST does its DB update, then `redirect('/', 303)`. PRG pattern — the browser's address bar lands back on `/`, refresh doesn't re-submit.

### Layout

Three sections, each a separate SQL query:

```
┌──────────────────────────────────────────────────────────┐
│  rne                          Worker: idle • 8s ago      │
│                               [Pause queue] [Resume]      │
├──────────────────────────────────────────────────────────┤
│  Running                                                  │
│  ─────────                                                │
│  S01E05  Initial D            ████████░░░░ 67% • 42 fps  │
│                                            ETA 00:08:14   │
│                                                           │
│  Queued (3)                                               │
│  ─────────                                                │
│  S01E06  Initial D                          [Pause]       │
│  S01E07  Initial D                          [Pause]       │
│  S01E08  Initial D                          [Pause]       │
│                                                           │
│  Recent (last 24h)                                        │
│  ─────────                                                │
│  S01E04  Initial D            done       2026-05-04 22:14│
│  S01E03  Initial D            failed     [Retry] exit 1  │
│  S01E02  Initial D            done       2026-05-04 18:02│
└──────────────────────────────────────────────────────────┘
```

- **Running** — the one currently encoding job. Live progress bar, fps, ETA. If no job is running, this section doesn't render. No "nothing running" placeholder.
- **Queued + Paused** — counted in the header, listed in `(priority, id)` order. Each row gets one button: `Pause` if queued, `Resume` if paused. Long queues scroll within the page.
- **Recent (last 24h)** — terminal jobs, newest first, capped at 50. `done` shows `finished_at`. `failed`/`interrupted`/`cancelled` show a Retry button and the `error_message` snippet. Older history queryable via `rne ls --all`.

### Header

Worker heartbeat indicator: green dot if `last_seen` within 30s, amber 30–90s, red beyond, "Worker offline" if stale beyond a couple of minutes. Pause/resume queue buttons toggle the global flag. When paused, the running job continues (matches worker behavior — pause is between-jobs only) but a banner reads "Queue paused".

### Mobile

Pico's defaults handle most of it. Two practical adjustments at narrow widths (`@media (max-width: 600px)`):

1. Hide the ETA column. Progress percentage is the headline number; ETA is detail.
2. Episode/show display becomes one line (`Initial D · S01E05`) instead of two columns.

Eight or so lines of CSS total. No mobile-specific templates.

### Auth

None. Bind to `0.0.0.0:8500`, add a pihole DNS record for `rne.lan`, done. Exposing beyond the LAN is a reverse-proxy + basic-auth concern, not the app's.

### Production server

Flask's dev server (`app.run`) is fine for personal LAN use. If it falls over in real use, swap in `waitress` (one line in `main()`, no other changes).

## systemd integration

Both units are installed as **user-level** services under `~/.config/systemd/user/` and managed with `systemctl --user`. They run as the invoking user implicitly — no `User=` or `Group=` directive is needed or valid in this context. Group access to `/mnt/media` is provided by the user's `mediagroup` membership and the setgid bit on `/mnt/media` directories, not via a `Group=` directive.

### `systemd/rne-worker.service`

```ini
[Unit]
Description=rne encoder worker
After=network.target
ConditionPathExists=/mnt/media

[Service]
Type=simple
ExecStart=__RNE_WORKER_BIN__
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
Nice=10
Environment=HOME=/home/rip
Environment=RNE_DB=/home/rip/.local/state/rne/jobs.db

[Install]
WantedBy=default.target
```

`__RNE_WORKER_BIN__` is substituted with the resolved binary path at install time by `rne service install`.

- `Restart=on-failure` brings the worker back automatically on crash.
- `RestartSec=5` prevents tight crash loops.
- `TimeoutStopSec=30` gives HandBrake time to wind down on SIGTERM.
- `Nice=10` deprioritizes the worker so it doesn't starve the dashboard or interactive SSH.
- `ConditionPathExists=/mnt/media` prevents the worker from starting before the 9p mount is ready — important on VM boot, where without this the worker could start, claim a job, and immediately fail because `/mnt/media` isn't mounted yet.

### `systemd/rne-dashboard.service`

```ini
[Unit]
Description=rne dashboard
After=network.target
ConditionPathExists=/mnt/media

[Service]
Type=simple
ExecStart=__RNE_DASHBOARD_BIN__
Restart=on-failure
RestartSec=5
Environment=HOME=/home/rip
Environment=RNE_DB=/home/rip/.local/state/rne/jobs.db

[Install]
WantedBy=default.target
```

`__RNE_DASHBOARD_BIN__` is substituted with the resolved binary path at install time by `rne service install`.

Same shape minus the nice boost and stop timeout. `ConditionPathExists` is arguably overkill here since the dashboard doesn't touch `/mnt/media` directly, but matching the worker's startup conditions keeps both from racing weird boot states.

### Install

```
rne service install
systemctl --user enable --now rne-worker rne-dashboard
```

`rne service install` resolves the `rne-worker` and `rne-dashboard` binaries from PATH, writes the unit files to `~/.config/systemd/user/`, and runs `systemctl --user daemon-reload`.

## Reliability properties

- **VM reboot mid-encode.** Worker's `running` row is reconciled to `interrupted` on next start. The `.partial` output sits on disk untouched. Dashboard surfaces with retry button.
- **Worker crash.** systemd `Restart=on-failure` brings it back within 5 seconds. Same orphan reconciliation runs.
- **Dashboard crash.** Restarted by systemd. Worker is unaffected. The DB is the only shared state.
- **Concurrent ingest while worker is running.** Fine — they don't share locks. Ingest writes new rows, worker may pick them up in the next claim cycle. Batch-scoped raw dirs (`_raw/batch-{id}/`) ensure that ripping disc 2 never overwrites disc 1's raw files while the worker is mid-encode.
- **Database write contention.** WAL mode + `busy_timeout=5000` handles it. Worker writes progress every ~20s; dashboard reads on every page load. Real conflict is rare.
- **`/mnt/media` not mounted at boot.** `ConditionPathExists` blocks both services from starting until it's there.

## Open questions to revisit during implementation

- How reliable is ffprobe stream duration as a forced-vs-full subtitle signal across the user's Blu-ray collection? If unreliable, fall back to `mkvmerge --identify --identification-format json` from `mkvtoolnix-cli`. Worth keeping in pocket.
- Is the 30-second dashboard meta-refresh too slow (hard to notice progress) or too fast (pointless reloads when nothing changes)? Easy to tune.
- Default `priority` is 0 for everything. If priority becomes useful (e.g. boost a particular show ahead of the queue), `rne priority <id> <n>` is a trivial add.
- `COPY_FRIENDLY_AUDIO_CODECS` and `AC3_BITRATE_BY_CHANNELS` are tunable — adjust if real-world testing shows DTS plays fine on your client devices, or if a different default bitrate is preferred for surround sources.
