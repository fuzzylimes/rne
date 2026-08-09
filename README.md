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
rsync -av dist/rne-0.4.0-py3-none-any.whl rip@rip:~/

# On VM: install and set up services
pipx install ~/rne-0.4.0-py3-none-any.whl
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

To write the output to a different drive, see [Choosing an output disk](#choosing-an-output-disk).

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
5. Rip-complete notification, if configured — see [Getting notified when a rip finishes](#getting-notified-when-a-rip-finishes)
6. Probe of first file — shows video/audio/subtitle track table
7. Audio tracks to encode, subtitle tracks, CRF quality, preset, detelecine (DVD + NTSC only), decomb. The preset default is `medium` for DVD sources (`mpeg2video` codec or `--dvd` flag) and `slow` for everything else (Blu-rays)
8. Preview of all queued jobs — confirm or edit before inserting

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

### Choosing an output disk

Both `rne ingest` and `rne queue` write encodes under a staging root. With more than one drive available, pick which one per invocation:

```bash
rne ingest -dk archive                    # a disk named in the config file
rne ingest --media-root /mnt/media2       # one-off; staging goes to /mnt/media2/staging
rne disks                                 # list configured disks
```

The two flags are mutually exclusive. With neither, output goes to the config file's `default_disk`, or to the built-in `/mnt/media/staging` if there is no config file.

Name your drives once in `~/.config/rne/config.toml` so you don't have to type paths:

```toml
default_disk = "media"

[disks.media]
media_root = "/mnt/media"

[disks.archive]
media_root = "/mnt/media2"
# staging_root defaults to {media_root}/staging; override it if you need to:
# staging_root = "/mnt/scratch/incoming"
```

The config file is entirely optional — without one, everything behaves as it did before. But if the file exists and has a problem (a typo'd key, a relative path, a `default_disk` that isn't defined), the command stops with an error rather than quietly falling back, so a typo can't send an eight-hour encode to the wrong drive.

Set `RNE_CONFIG` to use a config file somewhere other than `~/.config/rne/config.toml`.

### Getting notified when a rip finishes

Ripping is the part you have to be present for — once it's done the drive is
free and the disc can be swapped, but nothing tells you that from another room.
`rne ingest` can publish an MQTT message the moment ripping finishes (right
before the track-selection prompts), which Home Assistant can turn into a phone
push.

Add a `[notifications.mqtt]` table to the same config file:

```toml
[notifications.mqtt]
host     = "homeassistant.local"
username = "rne"
password = "..."
topic    = "rne/rip"
payload  = '{"disc": "{disc}", "title": "{title}", "titles": {count}}'
```

That's the minimum plus credentials — `host` and `topic` are the only required
keys. **The topic and payload are yours**; rne doesn't care what's in them. Use
a bare string if that's all you need:

```toml
topic   = "house/notify"
payload = "Disc done, go swap it"
```

The full set of keys:

| Key | Default | Notes |
|---|---|---|
| `host` | *required* | Broker hostname or IP — the machine running Mosquitto, usually Home Assistant itself |
| `topic` | *required* | Any topic you like. No `+` or `#` — those are for subscribing |
| `payload` | `"{title} rip complete"` | Any string. JSON is fine and needs no escaping |
| `port` | `1883` | `8883` is the convention for TLS |
| `username` / `password` | none | Omit both for an anonymous broker |
| `client_id` | `rne-<pid>` | Only matters if your broker filters on it |
| `qos` | `0` | `0` = fire and forget, `1` = wait for the broker to acknowledge. `2` is not supported |
| `retain` | `false` | `true` makes the broker keep the last message for new subscribers |
| `tls` | `false` | `tls_insecure = true` additionally skips certificate verification — only for a self-signed cert on a network you trust |
| `timeout` | `5.0` | Seconds. Caps how long ingest can pause on a broker that isn't answering |

Both `topic` and `payload` support these placeholders:

| Placeholder | Example |
|---|---|
| `{disc}` | `INITIAL_D_VOL3` — the disc's volume name |
| `{title}` | `Initial D` — the show/movie name you entered |
| `{kind}` | `tv` or `movie` |
| `{season}` | `1` (empty for movies) |
| `{count}` | `7` — titles ripped |
| `{batch}` | `17` — the batch id, matches `rne ls` |
| `{hostname}` | `rip` — the machine running rne |

Anything that isn't a recognised placeholder is left alone, so JSON braces need
no escaping and a typo like `{titel}` shows up literally in the message rather
than breaking it.

Since the file holds a broker password, lock it down:

```bash
chmod 600 ~/.config/rne/config.toml
```

**Setting this up in Home Assistant**, if you haven't used MQTT before:

1. Install the **Mosquitto broker** add-on (Settings → Add-ons → Add-on Store)
   and start it. Add the **MQTT** integration when Home Assistant offers it.
2. Create a normal Home Assistant user for rne to log in as (Settings → People →
   Add Person, "Allow person to login"). Mosquitto accepts HA users as MQTT
   credentials — put that username and password in the config file above.
3. Set `host` to your Home Assistant machine and leave `port` at `1883`.
4. Add an automation that listens on your topic and pushes to your phone. In
   Settings → Automations → Create → Edit in YAML:

   ```yaml
   alias: Disc rip finished
   triggers:
     - trigger: mqtt
       topic: rne/rip
   actions:
     - action: notify.mobile_app_<your_phone>
       data:
         title: Rip finished
         message: "{{ trigger.payload_json.title }} — {{ trigger.payload_json.titles }} titles ready"
   ```

   `trigger.payload_json` works when your `payload` is JSON. For a plain-string
   payload use `{{ trigger.payload }}` instead.

To check it end to end without burning a disc, watch the topic from the HA
host — Settings → Devices & Services → MQTT → Configure → Listen to a topic —
and run an ingest.

Ingest prints one line either way and **never stops for a notification
problem**:

```
Notification sent: rne/rip -> homeassistant.local:1883
Notification failed: homeassistant.local:1883: [Errno 111] Connection refused
```

If the broker is down, unreachable, or rejects the password, you get the second
line and the prompts continue as normal. With no `[notifications.mqtt]` table
at all, nothing is attempted and nothing is printed. A table that's there but
missing `host` or `topic` prints `Notification skipped: ...` and carries on —
but a *misspelled* key is treated like any other config typo and stops the
command with exit 2 before the disc spins up, so a silent `topc = "rne/rip"`
can't leave you waiting for a notification that was never going to arrive.

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

Two things come from `~/.config/rne/config.toml`: output locations, chosen per invocation — see [Choosing an output disk](#choosing-an-output-disk) — and [rip-complete notifications](#getting-notified-when-a-rip-finishes). Everything else lives as a constant in `src/rne/config.py`:

| Constant | Default | Notes |
|---|---|---|
| `STAGING_ROOT` | `/mnt/media/staging` | Fallback when no disk is selected; override with `RNE_STAGING_ROOT` env var |
| `CONFIG_PATH` | `~/.config/rne/config.toml` | Named output disks and notifications; override with `RNE_CONFIG` env var |
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
uv build            # build wheel → dist/rne-0.4.0-py3-none-any.whl
```

Tests use in-memory SQLite; no external binaries required.
