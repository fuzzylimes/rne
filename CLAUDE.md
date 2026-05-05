# rne

Personal media pipeline: disc → rip (MakeMKV) → encode (HandBrake) → queue.

## Read first

- `SPEC.md` — full design doc. Treat as the source of truth for architecture,
  schema, state machine, and module ownership. Do not deviate without flagging it.
- `reference/` — the user's original standalone scripts (`mkvrip`,
  `mkvprobe-format.py`) and a copy of the `batchbrake` project. These are
  reference material to port from, NOT current source. Logic in `src/rne/`
  supersedes anything in `reference/`.

## Project conventions

- Python 3.12+, `uv` for dependency management.
- Standard library by default. Only runtime dep is `flask`. Don't add
  dependencies without asking.
- No ORM. SQLite via stdlib `sqlite3` with `Row` factory.
- `db.py` owns all schema and common queries. Inline one-off queries are fine
  at call sites.
- Pure functions where possible (especially `handbrake.py`). Side-effecting
  code clearly separated.
- Tests with pytest, against in-memory SQLite for DB tests.

## Workflow

- Make small, reviewable changes. Stop at natural checkpoints for review
  rather than building entire subsystems in one pass.
- Run `ruff check` and `pytest` before declaring a piece done.
- When porting from `reference/`, preserve the working logic; reorganization
  for the new module layout is fine but don't "improve" semantics silently.

## Out of scope

- Auth, multi-user, parallel encoding, library management. See SPEC.md
  "Non-goals" for the full list.
