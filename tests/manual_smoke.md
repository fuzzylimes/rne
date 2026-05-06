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
