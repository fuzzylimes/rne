"""Tests for TVDB lookup: config, client + cache, year naming, and the lookup UI."""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest
from textual.widgets import DataTable, Input

from rne import config, db, jobs, naming, plan, tvdb
from rne.models import HandbrakeArgs
from rne.tui.edit import EditApp
from rne.tui.flow import Draft, Naming, Source
from rne.tui.lookup import LookupModal
from rne.tui.pipeline import NamingScreen, PipelineApp, ReviewScreen, TracksScreen
from tests.conftest import insert_job
from tests.test_jobs import SUMMARY

SEARCH = {
    "status": "success",
    "data": [
        {
            "objectID": "series-81189", "id": "series-81189", "tvdb_id": "81189",
            "name": "Breaking Bad", "year": "2008", "type": "series",
            "network": "AMC", "country": "usa",
            "overview": "A chemistry teacher...",
            "translations": {"eng": "Breaking Bad", "deu": "Breaking Bad DE"},
            "overviews": {"eng": "A high school chemistry   teacher\ndiagnosed..."},
        },
        {
            "id": "series-999", "name": "頭文字D", "first_air_time": "1998-04-18",
            "type": "series", "translations": {"eng": "Initial D"},
        },
        {"id": "series-bogus", "name": "no usable id"},
        {"tvdb_id": "5", "name": ""},  # nameless: skipped
        "junk",
    ],
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _write(tmp_path, text: str) -> pathlib.Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_tvdb_config_absent(tmp_path):
    assert config.load_tvdb_config(tmp_path / "missing.toml") == (None, None)
    assert config.load_tvdb_config(_write(tmp_path, "default_disk = 'x'\n")) == (None, None)


def test_tvdb_config_full(tmp_path):
    p = _write(tmp_path, '[tvdb]\napi_key = " abc "\nlanguage = "deu"\ncache_ttl_days = 0\n')
    assert config.load_tvdb_config(p) == (
        config.TvdbConfig(api_key="abc", language="deu", cache_ttl_days=0), None
    )


def test_tvdb_config_without_key_is_off_with_reason(tmp_path):
    assert config.load_tvdb_config(_write(tmp_path, "[tvdb]\nlanguage = 'eng'\n")) == (
        None, "[tvdb] has no api_key"
    )


@pytest.mark.parametrize("body", [
    "[tvdb]\napi_key = 'k'\napikey = 'typo'\n",
    "[tvdb]\napi_key = ''\n",
    "[tvdb]\napi_key = 'k'\ncache_ttl_days = -1\n",
    "[tvdb]\napi_key = 'k'\ncache_ttl_days = true\n",
    "tvdb = 'k'\n",
])
def test_tvdb_config_malformed(tmp_path, body):
    with pytest.raises(config.ConfigError):
        config.load_tvdb_config(_write(tmp_path, body))


def test_tvdb_table_is_a_known_top_level_key(tmp_path):
    p = _write(tmp_path, "[tvdb]\napi_key = 'k'\n")
    assert config.load_disk_config(p).disks == {}


# ---------------------------------------------------------------------------
# parse_search
# ---------------------------------------------------------------------------


def test_parse_search():
    matches = tvdb.parse_search(SEARCH, "eng")
    assert matches == [
        tvdb.Match(81189, "series", "Breaking Bad", 2008,
                   "A high school chemistry teacher diagnosed...", "AMC", "USA"),
        tvdb.Match(999, "series", "Initial D", 1998),
    ]
    assert matches[0].label() == "Breaking Bad (2008)"
    assert tvdb.parse_search(SEARCH, "deu")[0].name == "Breaking Bad DE"
    assert tvdb.parse_search({}, "eng") == []


# ---------------------------------------------------------------------------
# Client + cache
# ---------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, *, login_status=200, search_status=200):
        self.calls: list[tuple[str, str]] = []
        self.login_status = login_status
        self.search_statuses = [search_status] if isinstance(search_status, int) else search_status
        self.tokens = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("?")[0].rsplit("/", 1)[1]))
        if url.endswith("/login"):
            assert json.loads(body) == {"apikey": "key"}
            if self.login_status != 200:
                return self.login_status, {"message": "Unauthorized"}
            self.tokens += 1
            return 200, {"data": {"token": f"tok{self.tokens}"}}
        assert headers["Authorization"] == f"Bearer tok{self.tokens}"
        assert "query=Breaking+Bad" in url and "type=series" in url
        status = self.search_statuses.pop(0) if len(self.search_statuses) > 1 else self.search_statuses[0]
        return status, SEARCH if status == 200 else {"message": "nope"}


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jobs.db")
    conn = db.connect(path)
    db.init_db(conn)
    conn.close()
    return path


def _client(db_path, http, clock=None, ttl=7):
    clock = clock or [1000.0]
    return tvdb.Tvdb(config.TvdbConfig(api_key="key", cache_ttl_days=ttl),
                     connect=lambda: db.connect(db_path), http=http, now=lambda: clock[0])


def test_search_logs_in_and_caches(db_path):
    http = FakeHttp()
    clock = [1000.0]
    client = _client(db_path, http, clock)
    assert [m.tvdb_id for m in client.search("  Breaking   Bad ", "series")] == [81189, 999]
    assert http.calls == [("POST", "login"), ("GET", "search")]
    # Same query (any case/spacing): from the cache.
    client.search("breaking bad", "series")
    assert len(http.calls) == 2
    # Past the TTL: searches again, reusing the token.
    clock[0] += 8 * 86400
    client.search("Breaking Bad", "series")
    assert http.calls[2:] == [("GET", "search")]
    # Past the token's life: logs in again.
    clock[0] += 30 * 86400
    client.search("Breaking Bad", "series")
    assert http.calls[3:] == [("POST", "login"), ("GET", "search")]


def test_search_relogs_in_once_on_401(db_path):
    http = FakeHttp(search_status=[401, 200])
    client = _client(db_path, http)
    assert client.search("Breaking Bad", "series")
    assert http.calls == [("POST", "login"), ("GET", "search"), ("POST", "login"), ("GET", "search")]


def test_login_failure(db_path):
    with pytest.raises(tvdb.TvdbError, match="login failed.*check \\[tvdb\\] api_key"):
        _client(db_path, FakeHttp(login_status=401)).search("Breaking Bad", "series")


def test_search_failure_not_cached(db_path):
    http = FakeHttp(search_status=500)
    client = _client(db_path, http)
    with pytest.raises(tvdb.TvdbError, match="search failed"):
        client.search("Breaking Bad", "series")
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tvdb_cache WHERE key LIKE 'search:%'").fetchone()[0] == 0


def test_empty_query_skips_the_api(db_path):
    http = FakeHttp()
    assert _client(db_path, http).search("   ", "series") == []
    assert http.calls == []


def test_unreachable(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(tvdb.urllib.request, "urlopen", boom)
    with pytest.raises(tvdb.TvdbError, match="unreachable: Name or service not known"):
        tvdb.urllib_http("GET", "https://example.invalid", {}, None)


def test_migration_2(conn):
    assert db.schema_version(conn) >= 2
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"year", "tvdb_id"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM tvdb_cache").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Naming with a year
# ---------------------------------------------------------------------------


def test_folder_name():
    assert naming.folder_name("Heat", 1995) == "Heat (1995)"
    assert naming.folder_name("Heat") == "Heat"


def test_job_entries_with_year():
    root = pathlib.Path("/s")
    tv = plan.tv_job(show="Breaking Bad", season=1, episode=2,
                     show_dir=root / naming.folder_name("Breaking Bad", 2008),
                     source_path="/r/a.mkv", args=HandbrakeArgs(), year=2008, tvdb_id=81189)
    assert tv["output_path"] == "/s/Breaking Bad (2008)/Season 01/Breaking Bad - S01E02.mkv"
    assert (tv["year"], tv["tvdb_id"]) == (2008, 81189)
    movie = plan.movie_job(movie="Heat", movie_dir=root / "Heat (1995)", source_path="/r/b.mkv",
                           args=HandbrakeArgs(), year=1995, suffix="_t02")
    assert movie["output_path"] == "/s/Heat (1995)/Heat (1995)_t02.mkv"
    assert movie["label"] == "Heat (1995)" and movie["movie"] == "Heat"


@pytest.mark.parametrize("old,kw,new", [
    ("/s/Heat/Heat_t02.mkv", dict(new_year=1995), "/s/Heat (1995)/Heat (1995)_t02.mkv"),
    ("/s/Heat (1995)/Heat (1995).mkv", dict(old_year=1995), "/s/Heat/Heat.mkv"),
])
def test_renamed_output_movie_years(old, kw, new):
    out = naming.renamed_output(old, is_tv=False, old_title="Heat", new_title="Heat", **kw)
    assert out == pathlib.Path(new)


def test_renamed_output_tv_year():
    out = naming.renamed_output("/s/BB/Season 01/BB - S01E01.mkv", is_tv=True, old_title="BB",
                                new_title="Breaking Bad", season=1, episode=1, new_year=2008)
    assert out == pathlib.Path("/s/Breaking Bad (2008)/Season 01/Breaking Bad - S01E01.mkv")


def test_batch_label_and_insert_with_year(conn, tmp_path):
    batch = jobs.create_batch_row(conn, is_tv=True, show="Breaking Bad", movie=None, season=1,
                                  notes="", year=2008, tvdb_id=81189)
    row = conn.execute("SELECT label, year, tvdb_id FROM ingest_batches").fetchone()
    assert tuple(row) == ("Breaking Bad (2008) S01", 2008, 81189)
    entry = plan.tv_job(show="Breaking Bad", season=1, episode=1, show_dir=tmp_path,
                        source_path="/r/a.mkv", args=HandbrakeArgs(), year=2008, tvdb_id=81189)
    jobs.insert_jobs(conn, batch, [entry])
    assert tuple(conn.execute("SELECT year, tvdb_id FROM jobs").fetchone()) == (2008, 81189)


def test_draft_staging_dir_uses_year():
    d = Draft(mode="queue", naming=Naming(title="Breaking Bad", season=1, first_episode=1,
                                          year=2008, tvdb_id=81189),
              staging_root=pathlib.Path("/staging"),
              sources=[Source(pathlib.Path("/r/a.mkv"))])
    d.summaries = {pathlib.Path("/r/a.mkv"): SUMMARY}
    job = d.build_plan()[0]
    assert job["output_path"] == "/staging/Breaking Bad (2008)/Season 01/Breaking Bad - S01E01.mkv"
    assert (job["year"], job["tvdb_id"]) == (2008, 81189)
    assert Naming(title="X", is_tv=False, year=99).errors() == ["year must be a 4-digit year (or blank)"]


def test_plan_edits_year_keep_and_change(conn, tmp_path):
    job_id = insert_job(conn, movie="Heat", output_path=str(tmp_path / "Heat" / "Heat.mkv"))
    targets = jobs.load_edit_targets(conn, [job_id], probe_file=lambda p: {"streams": []})
    (kept,) = jobs.plan_edits(targets, template=None, title="Heat")
    assert (kept.year, kept.output_path) == (None, str(tmp_path / "Heat" / "Heat.mkv"))
    (changed,) = jobs.plan_edits(targets, template=None, title="Heat", year=1995, tvdb_id=7)
    assert changed.output_path == str(tmp_path / "Heat (1995)" / "Heat (1995).mkv")
    jobs.apply_edits(conn, [changed])
    assert tuple(conn.execute("SELECT year, tvdb_id FROM jobs").fetchone()) == (1995, 7)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def _run(app, steps):
    async def main():
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            await steps(pilot, app)
            await pilot.pause()
        return app.return_value
    return asyncio.run(main())


async def _settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


def fake_lookup(calls):
    def lookup(query, kind):
        calls.append((query, kind))
        if query == "fail":
            raise tvdb.TvdbError("TVDB is unreachable: timed out")
        return tvdb.parse_search(SEARCH, "eng")
    return lookup


def _queue_app(conn, tmp_path, lookup=None):
    src = tmp_path / "bb.mkv"
    src.touch()
    draft = Draft(mode="queue", naming=Naming(title="breaking bad"),
                  staging_root=tmp_path / "staging", sources=[Source(src)])
    return PipelineApp(draft, conn=conn, prober=lambda p: SUMMARY, chapter_prober=lambda p: [],
                       lookup=lookup, lookup_note=None if lookup else "[tvdb] has no api_key")


def test_naming_enter_searches_and_pick_fills_name_and_year(conn, tmp_path):
    calls = []
    app = _queue_app(conn, tmp_path, fake_lookup(calls))

    async def steps(pilot, app):
        await pilot.press("enter")  # Enter on the name: search
        await _settle(pilot)
        modal = app.screen
        assert isinstance(modal, LookupModal)
        assert calls == [("breaking bad", "series")]
        assert modal.query_one(DataTable).row_count == 2
        await pilot.press("enter")  # pick the first match
        await pilot.pause()
        naming_ = app.screen
        assert isinstance(naming_, NamingScreen)
        assert naming_.query_one("#title", Input).value == "Breaking Bad"
        assert naming_.query_one("#year", Input).value == "2008"
        assert "TVDB #81189" in str(naming_.query_one("#match").render())
        assert "staging/Breaking Bad (2008)" in str(naming_.query_one("#preview").render())
        assert app.focused.id == "season"  # next thing to answer
        naming_.query_one("#season", Input).value = "1"
        naming_.query_one("#first", Input).value = "1"
        await pilot.press("ctrl+n")
        await _settle(pilot)
        assert isinstance(app.screen, TracksScreen)
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        await pilot.press("ctrl+s")

    lines, ok = _run(app, steps)
    assert ok
    row = conn.execute("SELECT show, year, tvdb_id, output_path FROM jobs").fetchone()
    assert tuple(row)[:3] == ("Breaking Bad", 2008, 81189)
    assert row["output_path"].endswith("staging/Breaking Bad (2008)/Season 01/Breaking Bad - S01E01.mkv")
    batch = conn.execute("SELECT label, year, tvdb_id FROM ingest_batches").fetchone()
    assert tuple(batch) == ("Breaking Bad (2008) S01 (queued)", 2008, 81189)


def test_naming_edit_after_match_drops_tvdb_id_and_typed_keeps_name(conn, tmp_path):
    calls = []
    app = _queue_app(conn, tmp_path, fake_lookup(calls))

    async def steps(pilot, app):
        await pilot.press("enter")
        await _settle(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert app.draft.naming.tvdb_id == 81189
        title = app.screen.query_one("#title", Input)
        title.value = "Breaking Bad Remake"
        await pilot.pause()
        assert app.draft.naming.tvdb_id is None
        assert app.draft.naming.year == 2008  # year stays; it's a visible field
        title.focus()
        await pilot.press("enter")  # new name → searches again
        await _settle(pilot)
        assert len(calls) == 2
        await pilot.press("ctrl+t")  # use as typed
        await pilot.pause()
        assert app.screen.query_one("#title", Input).value == "Breaking Bad Remake"
        assert app.draft.naming.tvdb_id is None
        await pilot.press("ctrl+q")

    _run(app, steps)


def test_lookup_error_is_shown(conn, tmp_path):
    app = _queue_app(conn, tmp_path, fake_lookup([]))

    async def steps(pilot, app):
        app.screen.query_one("#title", Input).value = "fail"
        await pilot.press("enter")
        await _settle(pilot)
        assert "unreachable: timed out" in str(app.screen.query_one("#status").render())
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, NamingScreen)
        await pilot.press("ctrl+q")

    _run(app, steps)


def test_no_tvdb_key_enter_moves_on(conn, tmp_path):
    app = _queue_app(conn, tmp_path, None)

    async def steps(pilot, app):
        assert "TVDB lookup off: [tvdb] has no api_key" in str(app.screen.query_one("#match").render())
        app.screen.query_one("#season", Input).value = "1"
        app.screen.query_one("#first", Input).value = "1"
        app.screen.query_one("#title", Input).focus()
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, TracksScreen)
        await pilot.press("ctrl+q")

    _run(app, steps)


def test_edit_lookup_sets_year_and_renames(conn, tmp_path):
    job_id = insert_job(conn, show="breaking bad", season=1, episode=3,
                        output_path=str(tmp_path / "breaking bad" / "Season 01" / "breaking bad - S01E03.mkv"))
    conn.execute("UPDATE jobs SET source_probe = ?", (__import__("rne.probe").probe.snapshot_to_json(SUMMARY),))
    conn.commit()
    calls = []

    async def steps(pilot, app):
        await pilot.press("ctrl+f")
        await _settle(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#year", Input).value == "2008"
        await pilot.press("ctrl+s")

    lines, ok = _run(EditApp(conn, [job_id], lookup=fake_lookup(calls)), steps)
    assert ok
    row = conn.execute("SELECT show, year, tvdb_id, output_path FROM jobs").fetchone()
    assert tuple(row)[:3] == ("Breaking Bad", 2008, 81189)
    assert row["output_path"] == str(
        tmp_path / "Breaking Bad (2008)" / "Season 01" / "Breaking Bad - S01E03.mkv")
