import sqlite3

import pytest

from rne import db
from rne.models import HandbrakeArgs


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.init_db(c)
    yield c
    c.close()


_DEFAULT_HB_ARGS = HandbrakeArgs().to_json()


def insert_job(
    conn: sqlite3.Connection,
    *,
    movie: str = "Test Movie",
    show: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    source_path: str = "/staging/title_t00.mkv",
    output_path: str = "/staging/Test Movie.mkv",
    handbrake_args: str = _DEFAULT_HB_ARGS,
    status: str = "queued",
    priority: int = 0,
) -> int:
    # movie XOR show must be set to satisfy the CHECK constraint
    if show is not None:
        movie_val, show_val = None, show
    else:
        movie_val, show_val = movie, None

    cur = conn.execute(
        """
        INSERT INTO jobs (show, season, episode, movie,
                          source_path, output_path, handbrake_args,
                          status, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            show_val,
            season,
            episode,
            movie_val,
            source_path,
            output_path,
            handbrake_args,
            status,
            priority,
        ),
    )
    conn.commit()
    return cur.lastrowid
