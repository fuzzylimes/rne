"""Jellyfin Live TV recordings: .nfo reading, library folders, queueing, the API."""

from __future__ import annotations

import pathlib
import types

import pytest

from rne import config, db, plan, probe, recordings
from rne.config import ApiConfig, ConfigError, LibraryConfig
from rne.models import AudioTrack
from rne.recordings import Recording, RecordingError

EPISODE_NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>Juno</title>
  <aired>2026-10-23</aired>
  <episode>3</episode>
  <season>1</season>
  <dateadded>2026-10-23 02:00:00</dateadded>
  <genre>Drama</genre>
  <studio />
</episodedetails>
"""
SHOW_NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>Cupertino</title>
  <mpaa>Language</mpaa>
</tvshow>
"""
NO_NUMBERS_NFO = "<episodedetails><title>Week 8</title><aired>2026-10-23</aired></episodedetails>"

# A 1080i broadcast: stereo first, 5.1 second, and an MPEG audio track.
TS_PROBE = {"streams": [
    {"codec_type": "video", "codec_name": "mpeg2video", "width": 1920, "height": 1080,
     "r_frame_rate": "30000/1001", "field_order": "tt"},
    {"codec_type": "audio", "codec_name": "ac3", "channels": 2, "tags": {"language": "eng"}},
    {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
    {"codec_type": "audio", "codec_name": "mp2", "channels": 2, "tags": {"language": "spa"}},
]}


def fake_probe(path: str) -> dict:
    return TS_PROBE


@pytest.fixture
def rec_root(tmp_path):
    root = tmp_path / "Recordings"
    show = root / "Series" / "Cupertino"
    show.mkdir(parents=True)
    (show / "tvshow.nfo").write_text(SHOW_NFO)
    ts = show / "Cupertino S01E03 Juno.ts"
    ts.write_bytes(b"\x47" * 188)
    ts.with_suffix(".nfo").write_text(EPISODE_NFO)
    return root


def _episode(root) -> pathlib.Path:
    return root / "Series" / "Cupertino" / "Cupertino S01E03 Juno.ts"


# ---------------------------------------------------------------------------
# .nfo parsing and describe()
# ---------------------------------------------------------------------------


def test_parse_nfos():
    assert recordings.parse_episode_nfo(EPISODE_NFO) == (1, 3)
    assert recordings.parse_episode_nfo(NO_NUMBERS_NFO) == (None, None)
    assert recordings.parse_show_nfo(SHOW_NFO) == ("Cupertino", None)
    assert recordings.parse_show_nfo(
        "<tvshow><title>X</title><premiered>2025-01-09</premiered></tvshow>") == ("X", 2025)
    assert recordings.parse_movie_nfo(
        "﻿<movie><title>Heat</title><year>1995</year></movie>") == ("Heat", 1995)
    with pytest.raises(RecordingError, match="not valid XML"):
        recordings.parse_episode_nfo("<episodedetails>")


def test_describe_series_episode(rec_root):
    assert recordings.describe(_episode(rec_root), rec_root) == Recording(
        is_tv=True, title="Cupertino", year=None, season=1, episode=3)


def test_describe_falls_back_to_folder_name_without_tvshow_nfo(rec_root):
    (rec_root / "Series" / "Cupertino" / "tvshow.nfo").unlink()
    show = rec_root / "Series" / "Show: The Return (2024)"
    show.mkdir()
    ts = show / "ep.ts"
    ts.write_bytes(b"")
    ts.with_suffix(".nfo").write_text(EPISODE_NFO)
    assert recordings.describe(_episode(rec_root), rec_root).title == "Cupertino"
    rec = recordings.describe(ts, rec_root)
    assert (rec.title, rec.year) == ("Show The Return", 2024)  # ':' is unsafe in paths


def test_describe_skips_sports_and_unknown_places(rec_root):
    ts = rec_root / "Series" / "Cupertino" / "Football.ts"
    ts.write_bytes(b"")
    assert "no episode .nfo" in recordings.describe(ts, rec_root)
    ts.with_suffix(".nfo").write_text(NO_NUMBERS_NFO)
    assert "no season and episode" in recordings.describe(ts, rec_root)
    other = rec_root / "Other" / "x.ts"
    other.parent.mkdir()
    other.write_bytes(b"")
    assert "Series/ or Movies/" in recordings.describe(other, rec_root)


def test_describe_refuses_paths_outside_root_or_missing(rec_root, tmp_path):
    outside = tmp_path / "elsewhere.ts"
    outside.write_bytes(b"")
    with pytest.raises(RecordingError, match="not under"):
        recordings.describe(outside, rec_root)
    with pytest.raises(RecordingError, match="not under"):
        recordings.describe(rec_root / "Series" / ".." / ".." / "elsewhere.ts", rec_root)
    with pytest.raises(RecordingError, match="not found"):
        recordings.describe(rec_root / "Series" / "Cupertino" / "gone.ts", rec_root)
    with pytest.raises(RecordingError, match="absolute"):
        recordings.describe(pathlib.Path("Series/x.ts"), rec_root)


def test_describe_movies(rec_root):
    movies = rec_root / "Movies"
    (movies / "Heat (1995)").mkdir(parents=True)
    with_nfo = movies / "Heat (1995)" / "Heat.ts"
    with_nfo.write_bytes(b"")
    with_nfo.with_suffix(".nfo").write_text("<movie><title>Heat</title><year>1995</year></movie>")
    assert recordings.describe(with_nfo, rec_root) == Recording(False, "Heat", 1995)
    loose = movies / "Ronin (1998).ts"
    loose.write_bytes(b"")
    assert recordings.describe(loose, rec_root) == Recording(False, "Ronin", 1998)


# ---------------------------------------------------------------------------
# Library folder reuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("existing, year, expected", [
    ([], None, ("Cupertino", None)),
    ([], 2025, ("Cupertino (2025)", 2025)),
    (["Cupertino (2025)"], None, ("Cupertino (2025)", 2025)),
    (["cupertino"], None, ("cupertino", None)),
    (["Cupertino", "Cupertino (2025)"], None, ("Cupertino", None)),
    (["Cupertino (1999)", "Cupertino (2025)"], 2025, ("Cupertino (2025)", 2025)),
    (["Cupertino (1999)", "Cupertino (2025)"], None, ("Cupertino", None)),  # can't tell which
    (["Cupertino Nights"], None, ("Cupertino", None)),
])
def test_find_library_folder(tmp_path, existing, year, expected):
    for name in existing:
        (tmp_path / name).mkdir()
    folder, found_year = recordings.find_library_folder(tmp_path, "Cupertino", year)
    assert (folder.name, found_year) == expected
    assert folder.parent == tmp_path


def test_livetv_args_keep_all_audio_drop_subs_decomb_interlaced():
    args = plan.livetv_args(probe.summarize(TS_PROBE))
    assert args.audio_tracks == [AudioTrack(1), AudioTrack(2), AudioTrack(3, "ac3", 192)]
    assert args.subtitle_tracks == [] and args.decomb and not args.detelecine
    assert (args.quality, args.preset) == (24, "slow")
    progressive = {"streams": [dict(TS_PROBE["streams"][0], field_order="progressive")]}
    assert not plan.livetv_args(probe.summarize(progressive)).decomb


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


def _enqueue(conn, path, rec_root, tmp_path, library=None):
    return recordings.enqueue(
        conn, path, recordings_root=rec_root, staging_root=tmp_path / "staging",
        library=library or LibraryConfig(tv_root=tmp_path / "Shows"), probe_file=fake_probe,
    )


def test_enqueue_reuses_library_folder_and_its_year(conn, rec_root, tmp_path):
    (tmp_path / "Shows" / "Cupertino (2025)").mkdir(parents=True)
    out = _enqueue(conn, _episode(rec_root), rec_root, tmp_path)
    assert (out.status, out.label) == ("queued", "S01E03")
    row = conn.execute("SELECT j.*, b.label AS batch FROM jobs j "
                       "JOIN ingest_batches b ON b.id = j.ingest_batch_id").fetchone()
    assert row["id"] == out.job_id and row["status"] == "queued"
    assert row["source_path"] == str(_episode(rec_root))
    assert row["output_path"] == str(
        tmp_path / "staging/Cupertino (2025)/Season 01/Cupertino - S01E03.mkv")
    assert row["final_path"] == str(
        tmp_path / "Shows/Cupertino (2025)/Season 01/Cupertino - S01E03.mkv")
    assert (row["show"], row["season"], row["episode"], row["year"]) == ("Cupertino", 1, 3, 2025)
    assert row["batch"] == "Cupertino (2025) S01 (Live TV)"
    assert row["source_probe"] and '"decomb": true' in row["handbrake_args"]
    assert _episode(rec_root).exists()  # never touched


def test_enqueue_new_show_without_year_and_no_library(conn, rec_root, tmp_path):
    out = _enqueue(conn, _episode(rec_root), rec_root, tmp_path)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (out.job_id,)).fetchone()
    assert row["final_path"] == str(tmp_path / "Shows/Cupertino/Season 01/Cupertino - S01E03.mkv")
    conn.execute("DELETE FROM jobs")
    conn.commit()
    out = _enqueue(conn, _episode(rec_root), rec_root, tmp_path, library=LibraryConfig())
    assert conn.execute("SELECT final_path FROM jobs WHERE id = ?",
                        (out.job_id,)).fetchone()[0] is None


def test_enqueue_twice_and_same_episode_recorded_again(conn, rec_root, tmp_path):
    first = _enqueue(conn, _episode(rec_root), rec_root, tmp_path)
    again = _enqueue(conn, _episode(rec_root), rec_root, tmp_path)
    assert (again.status, again.job_id) == ("exists", first.job_id)
    rerun = rec_root / "Series" / "Cupertino" / "Cupertino S01E03 Juno (rerun).ts"
    rerun.write_bytes(b"")
    rerun.with_suffix(".nfo").write_text(EPISODE_NFO)
    out = _enqueue(conn, rerun, rec_root, tmp_path)
    assert out.status == "skipped" and f"already job {first.job_id}" in out.reason
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_enqueue_skips_without_numbers_and_reports_probe_failure(conn, rec_root, tmp_path):
    ts = rec_root / "Series" / "Cupertino" / "Football.ts"
    ts.write_bytes(b"")
    ts.with_suffix(".nfo").write_text(NO_NUMBERS_NFO)
    assert _enqueue(conn, ts, rec_root, tmp_path).status == "skipped"

    def broken(path):
        raise RuntimeError("Invalid data found when processing input")

    with pytest.raises(RecordingError, match="ffprobe could not read"):
        recordings.enqueue(conn, _episode(rec_root), recordings_root=rec_root,
                           staging_root=tmp_path, library=LibraryConfig(), probe_file=broken)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Config, API and CLI
# ---------------------------------------------------------------------------

TOKEN = "0123456789abcdef0123"


def _write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_load_api_config(tmp_path):
    assert config.load_api_config(tmp_path / "missing.toml") is None
    good = _write_config(tmp_path, f'[api]\ntoken = "{TOKEN}"\nrecordings_root = "/mnt/rec"\n')
    assert config.load_api_config(good) == ApiConfig(TOKEN, pathlib.Path("/mnt/rec"))
    for body, message in [
        ('[api]\nrecordings_root = "/r"\n', "token is required"),
        ('[api]\ntoken = "short"\nrecordings_root = "/r"\n', "at least 16"),
        (f'[api]\ntoken = "{TOKEN}"\nrecordings_root = "rel"\n', "absolute"),
        (f'[api]\ntoken = "{TOKEN}"\nrecordings_root = "/r"\nport = 1\n', "unknown key"),
    ]:
        with pytest.raises(ConfigError, match=message):
            config.load_api_config(_write_config(tmp_path, body))


@pytest.fixture
def api_client(tmp_path, monkeypatch, rec_root):
    db_path = str(tmp_path / "jobs.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    cfg = _write_config(tmp_path, f"""
[api]
token = "{TOKEN}"
recordings_root = "{rec_root}"

[library]
tv_root = "{tmp_path / 'Shows'}"
""")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(config, "STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setattr(probe, "probe", fake_probe)
    from rne.dashboard.app import create_app

    conn = db.connect(db_path)
    db.init_db(conn)
    yield create_app().test_client(), conn
    conn.close()


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_api_queues_with_form_or_json(api_client, rec_root):
    client, conn = api_client
    path = str(_episode(rec_root))
    assert client.post("/api/v1/recordings", data={"path": path}).status_code == 401
    assert client.post("/api/v1/recordings", data={"path": path},
                       headers=_auth("x" * 20)).status_code == 401
    resp = client.post("/api/v1/recordings", data={"path": path}, headers=_auth())
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "queued" and body["label"] == "S01E03"
    resp = client.post("/api/v1/recordings", json={"path": path}, headers=_auth())
    assert resp.status_code == 200 and resp.get_json()["status"] == "exists"
    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["final_path"].endswith("Shows/Cupertino/Season 01/Cupertino - S01E03.mkv")


def test_api_errors(api_client, rec_root, tmp_path):
    client, _ = api_client
    assert client.post("/api/v1/recordings", headers=_auth()).status_code == 400
    resp = client.post("/api/v1/recordings", data={"path": str(tmp_path / "x.ts")},
                       headers=_auth())
    assert resp.status_code == 422 and "not under" in resp.get_json()["error"]
    ts = rec_root / "Series" / "Cupertino" / "Football.ts"
    ts.write_bytes(b"")
    ts.with_suffix(".nfo").write_text(NO_NUMBERS_NFO)
    resp = client.post("/api/v1/recordings", data={"path": str(ts)}, headers=_auth())
    assert resp.status_code == 200 and resp.get_json()["status"] == "skipped"


def test_api_off_without_config(api_client, tmp_path, monkeypatch):
    client, _ = api_client
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "none.toml"))
    resp = client.post("/api/v1/recordings", data={"path": "/x"}, headers=_auth())
    assert resp.status_code == 404 and "off" in resp.get_json()["error"]


def test_cli_add_recording(api_client, rec_root, tmp_path, capsys):
    from rne.cli.manage import run_add_recording

    _, conn = api_client
    roots = config.Roots(media_root=tmp_path, staging_root=tmp_path / "staging")
    args = types.SimpleNamespace(paths=[str(_episode(rec_root))], root=None)
    run_add_recording(args, roots)
    assert "queued as job" in capsys.readouterr().out
    run_add_recording(args, roots)
    assert "exists" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_add_recording(types.SimpleNamespace(paths=[str(tmp_path / "x.ts")], root=None), roots)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
