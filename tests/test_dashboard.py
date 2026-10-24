"""Dashboard: queue order, the edit form, and the pages and actions."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from werkzeug.datastructures import MultiDict

from rne import config, db, jobs, probe
from rne.dashboard.forms import EditForm
from rne.models import AudioTrack, HandbrakeArgs, SubtitleTrack
from tests.conftest import insert_job
from tests.test_edit import TWO_SUBS  # truehd 5.1 + ac3 2.0; two PGS subtitles


def _ids(conn):
    return jobs.waiting_job_ids(conn)


# ---------------------------------------------------------------------------
# Queue order
# ---------------------------------------------------------------------------


def test_reorder_sets_claim_order_and_new_jobs_go_last(conn):
    a, b, c = (insert_job(conn) for _ in range(3))
    assert jobs.reorder(conn, [c, a, b]) == [c, a, b]
    later = insert_job(conn)  # priority 0, queued after the reorder
    assert _ids(conn) == [c, a, b, later]
    claimed = db.claim_next_job(conn)
    assert claimed is not None and claimed.id == c


def test_reorder_ignores_unknown_and_started_ids_and_keeps_unlisted(conn):
    a, b, c = (insert_job(conn) for _ in range(3))
    done = insert_job(conn, status="done")
    paused = insert_job(conn, status="paused")
    # A stale page: doesn't know about `paused`, still lists `done` and a deleted id.
    assert jobs.reorder(conn, [b, done, 999, a]) == [b, a, c, paused]
    assert conn.execute("SELECT priority FROM jobs WHERE id = ?", (done,)).fetchone()[0] == 0


def test_move_to_top_and_batch_ids(conn):
    batch = jobs.create_batch_row(conn, is_tv=True, show="S", movie=None, season=1, notes="")
    a = insert_job(conn)
    e2 = insert_job(conn, show="S", season=1, episode=2)
    e1 = insert_job(conn, show="S", season=1, episode=1)
    conn.execute("UPDATE jobs SET ingest_batch_id = ? WHERE id IN (?, ?)", (batch, e1, e2))
    conn.commit()
    assert jobs.batch_waiting_ids(conn, batch) == [e1, e2]
    jobs.move_to_top(conn, jobs.batch_waiting_ids(conn, batch))
    assert _ids(conn) == [e1, e2, a]
    jobs.move_to_top(conn, [a])
    assert _ids(conn) == [a, e1, e2]


# ---------------------------------------------------------------------------
# The edit form, as a browser would submit it
# ---------------------------------------------------------------------------


class _FormReader(HTMLParser):
    """Collects what a browser would submit from the first <form>."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[tuple[str, str]] = []
        self._select: str | None = None
        self._options: list[tuple[str, bool]] = []
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._in_form = True
        if not self._in_form or "disabled" in a:
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((a["name"], a.get("value", "on")))
            else:
                self.fields.append((a["name"], a.get("value", "")))
        elif tag == "select":
            self._select, self._options = a.get("name"), []
        elif tag == "option" and self._select:
            self._options.append((a.get("value", ""), "selected" in a))

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        if tag == "select" and self._select:
            chosen = next((v for v, sel in self._options if sel), self._options[0][0])
            self.fields.append((self._select, chosen))
            self._select = None


def submitted(html: str) -> MultiDict:
    reader = _FormReader()
    reader.feed(html)
    return MultiDict(reader.fields)


def _set(data: MultiDict, name: str, value) -> MultiDict:
    data.setlist(name, [str(v) for v in value] if isinstance(value, list) else [str(value)])
    return data


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "no-config.toml"))
    conn = db.connect(path)
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(file_db):
    from rne.dashboard.app import create_app

    app = create_app()
    app.testing = True
    return app.test_client()


def _with_snapshot(conn, job_id, summary=TWO_SUBS, **cols):
    sets = {"source_probe": probe.snapshot_to_json(summary), **cols}
    conn.execute(
        f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), job_id),
    )
    conn.commit()


def _tv_job(conn, episode=1, args=None, root="/s", **cols):
    args = args or HandbrakeArgs(audio_tracks=[AudioTrack(2)])
    job_id = insert_job(
        conn, show="Show", season=1, episode=episode, handbrake_args=args.to_json(),
        output_path=f"{root}/Show/Season 01/Show - S01E{episode:02d}.mkv",
    )
    _with_snapshot(conn, job_id, **cols)
    return job_id


def _row(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def test_untouched_form_changes_nothing(file_db, client):
    job_id = _tv_job(file_db, episode=3, year=2025, tvdb_id=77,
                     final_path="/m/Shows/Show (2025)/Season 01/Show - S01E03.mkv")
    before = dict(_row(file_db, job_id))
    page = client.get(f"/jobs/{job_id}/edit").get_data(as_text=True)
    targets = jobs.load_edit_targets(file_db, [job_id])
    results, errors = EditForm.from_form(targets, submitted(page)).plan()
    assert errors == []
    (r,) = results
    assert (r.output_path, r.final_path, r.year, r.tvdb_id) == (
        before["output_path"], before["final_path"], 2025, 77)
    assert r.args.to_json() == before["handbrake_args"]


def test_form_reads_track_order_codecs_and_subtitles(file_db, client):
    job_id = _tv_job(file_db)
    data = submitted(client.get(f"/jobs/{job_id}/edit").get_data(as_text=True))
    assert data.getlist("audio_order") == ["2", "1"]  # kept tracks first
    _set(data, "audio_order", [1, 2])  # ▲ on track 1
    _set(data, "audio_include", [1, 2])
    _set(data, "audio_codec_1", "eac3")
    _set(data, "audio_bitrate_1", 448)
    _set(data, "sub_default", 2)  # picking a default also keeps the track
    _set(data, "quality", 18)
    _set(data, "tune", "animation")
    targets = jobs.load_edit_targets(file_db, [job_id])
    (r,), errors = EditForm.from_form(targets, data).plan()
    assert errors == []
    assert r.args.audio_tracks == [AudioTrack(1, "eac3", 448), AudioTrack(2)]
    assert r.args.subtitle_tracks == [SubtitleTrack(2, default=True)]
    assert (r.args.quality, r.args.tune) == (18, "animation")


def test_form_errors_are_reported_not_raised(file_db):
    job_id = _tv_job(file_db)
    targets = jobs.load_edit_targets(file_db, [job_id])
    data = MultiDict([
        ("title", ""), ("year", "20"), ("season", "x"), ("episode", "0"),
        ("audio_order", "1"), ("audio_order", "2"), ("audio_include", "1"),
        ("audio_codec_1", "ac3"), ("audio_bitrate_1", ""), ("quality", "high"),
        ("initial_title", "Show"),
    ])
    _, errors = EditForm.from_form(targets, data).plan()
    text = " ".join(errors)
    for expected in ("pick a bitrate", "Quality must be a number", "Name can't be empty",
                     "4-digit year", "Season", "Episode"):
        assert expected in text


def test_form_tvdb_and_library_choices(file_db, client):
    job_id = _tv_job(file_db, year=2025, tvdb_id=77,
                     final_path="/m/Shows/Show (2025)/Season 01/Show - S01E01.mkv")
    page = client.get(f"/jobs/{job_id}/edit").get_data(as_text=True)
    targets = jobs.load_edit_targets(file_db, [job_id])

    def plan_with(**changes):
        data = submitted(page)
        for k, v in changes.items():
            _set(data, k, v)
        (r,), errors = EditForm.from_form(targets, data).plan()
        assert errors == []
        return r

    renamed = plan_with(title="Other")
    assert renamed.tvdb_id is None  # a new name drops the old match
    assert renamed.final_path == "/m/Shows/Other (2025)/Season 01/Other - S01E01.mkv"
    picked = plan_with(title="Other", tvdb_id=99, tvdb_title="Other", year=2020)
    assert picked.tvdb_id == 99 and "Other (2020)" in picked.final_path
    data = submitted(page)
    data.poplist("move")
    (r,), _ = EditForm.from_form(targets, data).plan()
    assert r.final_path is None
    assert plan_with(library="/elsewhere/Show").final_path == \
        "/elsewhere/Show/Season 01/Show - S01E01.mkv"
    data = _set(submitted(page), "library", "relative/path")
    _, errors = EditForm.from_form(targets, data).plan()
    assert any("absolute path" in e for e in errors)


# ---------------------------------------------------------------------------
# Pages and actions
# ---------------------------------------------------------------------------


def test_index_and_live_partial(file_db, client):
    a = insert_job(file_db, movie="Heat")
    insert_job(file_db, movie="Ronin", status="paused")
    page = client.get("/").get_data(as_text=True)
    assert 'id="live"' in page and "Sortable.min.js" in page
    assert 'http-equiv="refresh"' in page.split("<noscript>")[1]  # only as the no-JS fallback
    live = client.get("/partials/live").get_data(as_text=True)
    assert f'data-id="{a}"' in live and "Heat" in live and "Resume" in live
    assert "<html" not in live


def test_job_page_shows_tracks_and_command(file_db, client):
    job_id = _tv_job(file_db, final_path="/m/Shows/Show/Season 01/Show - S01E01.mkv")
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "HandBrakeCLI" in page and "S01E01.mkv.partial" in page
    assert "not kept: 1: truehd" in page and "← 2: ac3" in page
    assert "/m/Shows/Show/Season 01/Show - S01E01.mkv" in page and "Run next" not in page
    assert client.get("/jobs/999").status_code == 404


def test_edit_saves_and_redirects(file_db, client, tmp_path):
    job_id = _tv_job(file_db, root=tmp_path)
    data = submitted(client.get(f"/jobs/{job_id}/edit").get_data(as_text=True))
    _set(data, "title", "New Show")
    _set(data, "episode", 4)
    resp = client.post(f"/jobs/{job_id}/edit", data=data)
    assert resp.status_code == 303 and resp.headers["Location"] == f"/jobs/{job_id}"
    row = _row(file_db, job_id)
    assert (row["show"], row["episode"]) == ("New Show", 4)
    assert row["output_path"] == f"{tmp_path}/New Show/Season 01/New Show - S01E04.mkv"
    assert "updated" in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_edit_with_errors_shows_form_again(file_db, client):
    job_id = _tv_job(file_db)
    data = _set(submitted(client.get(f"/jobs/{job_id}/edit").get_data(as_text=True)),
                "title", "")
    resp = client.post(f"/jobs/{job_id}/edit", data=data)
    assert resp.status_code == 422
    assert "Name can&#39;t be empty" in resp.get_data(as_text=True)
    assert _row(file_db, job_id)["show"] == "Show"


def test_edit_preview_fragment(file_db, client):
    job_id = _tv_job(file_db)
    data = _set(submitted(client.get(f"/jobs/{job_id}/edit").get_data(as_text=True)),
                "episode", 9)
    resp = client.post(f"/jobs/{job_id}/edit", data=data, headers={"X-Rne-Preview": "1"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Show - S01E09.mkv" in html and "<form" not in html
    assert _row(file_db, job_id)["episode"] == 1


def test_running_job_cannot_be_edited(file_db, client):
    job_id = _tv_job(file_db)
    file_db.execute("UPDATE jobs SET status = 'running'")
    file_db.commit()
    resp = client.get(f"/jobs/{job_id}/edit")
    assert resp.status_code == 303
    assert "encoding right now" in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_batch_edit_renumbers_and_keeps_numbering_if_a_job_starts(file_db, client, tmp_path):
    batch = jobs.create_batch_row(file_db, is_tv=True, show="Show", movie=None, season=1,
                                  notes="")
    ids = [_tv_job(file_db, episode=e, root=tmp_path) for e in (1, 2, 3)]
    file_db.execute("UPDATE jobs SET ingest_batch_id = ?", (batch,))
    file_db.commit()
    page = client.get(f"/batches/{batch}/edit").get_data(as_text=True)
    assert "3 jobs" in page
    data = _set(submitted(page), "episode", 5)
    file_db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (ids[0],))
    file_db.commit()
    resp = client.post(f"/batches/{batch}/edit?next=/", data=data)
    assert resp.status_code == 303
    episodes = [_row(file_db, i)["episode"] for i in ids]
    assert episodes == [1, 6, 7]  # the running job is left alone, the others keep their place


def test_queue_order_top_cancel_and_async(file_db, client):
    a, b, c = (insert_job(file_db) for _ in range(3))
    assert client.post("/queue/order", data={"ids": [str(c), str(b), str(a)]}).status_code == 204
    assert _ids(file_db) == [c, b, a]
    resp = client.post(f"/jobs/{a}/top", data={"next": f"/jobs/{a}"})
    assert resp.status_code == 303 and resp.headers["Location"] == f"/jobs/{a}"
    assert _ids(file_db) == [a, c, b]
    assert client.post(f"/jobs/{b}/pause", headers={"X-Rne-Async": "1"}).status_code == 204
    assert _row(file_db, b)["status"] == "paused"
    client.post(f"/jobs/{c}/cancel")
    assert _row(file_db, c)["status"] == "cancelled"
    # `next` must stay on this site.
    assert client.post(f"/jobs/{b}/resume", data={"next": "//evil.example/"}).headers[
        "Location"] == "/"


def test_tvdb_search_fragment(file_db, client, monkeypatch):
    from rne import tvdb

    def fake_lookup(query, kind):
        return [tvdb.Match(tvdb_id=5, name="Show: Reboot", year=2025, kind=kind,
                           network="HBO", country="usa", overview="A show.")]

    monkeypatch.setattr(tvdb, "lookup_from_config", lambda: (fake_lookup, None))
    html = client.get("/tvdb/search?q=show&kind=series").get_data(as_text=True)
    assert 'data-id="5"' in html and 'data-name="Show Reboot"' in html and "HBO" in html
    monkeypatch.setattr(tvdb, "lookup_from_config", lambda: (None, "no api_key"))
    assert "lookup is off" in client.get("/tvdb/search?q=x").get_data(as_text=True)
