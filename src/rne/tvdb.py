"""TVDB v4 lookup for show and movie names (the same API jfvnamer uses).

parse_search() is pure. Tvdb does the HTTP (stdlib urllib) and caches the
login token and search results in the tvdb_cache table, so a repeated search
and a month of logins cost nothing.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from rne.config import TvdbConfig

BASE_URL = "https://api4.thetvdb.com/v4"
TOKEN_TTL = 25 * 86400  # tokens last a month; refresh a little early
HTTP_TIMEOUT = 10.0

# (method, url, headers, body) -> (status, parsed JSON)
HttpFn = Callable[[str, str, dict, bytes | None], tuple[int, dict]]


class TvdbError(Exception):
    """The lookup failed; the message is fit to show the user."""


@dataclass(frozen=True)
class Match:
    tvdb_id: int
    kind: str  # "series" or "movie"
    name: str
    year: int | None
    overview: str = ""
    network: str = ""
    country: str = ""

    def label(self) -> str:
        return f"{self.name} ({self.year})" if self.year else self.name


def _year(item: dict) -> int | None:
    for value in (item.get("year"), (item.get("first_air_time") or "")[:4]):
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def parse_search(payload: dict, language: str) -> list[Match]:
    """Pure: TVDB /search response → matches, names in `language` where known."""
    matches = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("tvdb_id") or str(item.get("id", "")).rpartition("-")[2]
        try:
            tvdb_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        translations = item.get("translations") or {}
        overviews = item.get("overviews") or {}
        name = translations.get(language) or item.get("name") or ""
        if not name:
            continue
        overview = overviews.get(language) or item.get("overview") or ""
        matches.append(Match(
            tvdb_id=tvdb_id,
            kind=item.get("type") or "",
            name=name.strip(),
            year=_year(item),
            overview=" ".join(overview.split()),
            network=item.get("network") or "",
            country=(item.get("country") or "").upper(),
        ))
    return matches


def urllib_http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TvdbError(f"TVDB is unreachable: {reason}") from exc
    except ValueError as exc:
        raise TvdbError(f"TVDB sent an unreadable reply: {exc}") from exc


def lookup_from_config() -> tuple[Callable[[str, str], list[Match]] | None, str | None]:
    """(search function, None) when [tvdb] has a key; (None, why-not) otherwise.

    Raises ConfigError for a malformed [tvdb] table.
    """
    from rne import config, db

    cfg, note = config.load_tvdb_config()
    if cfg is None:
        return None, note
    return Tvdb(cfg, connect=db.connect).search, None


class Tvdb:
    """A TVDB client that caches in the rne database.

    connect opens a database connection; one is opened per call so lookups
    can run on a worker thread.
    """

    def __init__(
        self,
        config: TvdbConfig,
        connect: Callable[[], sqlite3.Connection],
        *,
        http: HttpFn = urllib_http,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.connect = connect
        self.http = http
        self.now = now

    # -- cache ---------------------------------------------------------------------

    def _cached(self, key: str, max_age: float) -> dict | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT body, fetched_at FROM tvdb_cache WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or self.now() - row[1] > max_age:
            return None
        return json.loads(row[0])

    def _store(self, key: str, body: dict) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tvdb_cache (key, body, fetched_at) VALUES (?, ?, ?)",
                (key, json.dumps(body), self.now()),
            )
            conn.commit()
        finally:
            conn.close()

    def _forget(self, key: str) -> None:
        conn = self.connect()
        try:
            conn.execute("DELETE FROM tvdb_cache WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()

    # -- API -----------------------------------------------------------------------

    def _token(self) -> str:
        cached = self._cached("token", TOKEN_TTL)
        if cached:
            return cached["token"]
        body: dict = {"apikey": self.config.api_key}
        if self.config.pin:
            body["pin"] = self.config.pin
        status, payload = self.http(
            "POST", f"{BASE_URL}/login",
            {"Content-Type": "application/json", "Accept": "application/json"},
            json.dumps(body).encode(),
        )
        token = (payload.get("data") or {}).get("token") if status == 200 else None
        if not token:
            message = payload.get("message") or f"HTTP {status}"
            raise TvdbError(f"TVDB login failed ({message}); check [tvdb] api_key")
        self._store("token", {"token": token})
        return token

    def search(self, query: str, kind: str) -> list[Match]:
        """kind is "series" or "movie". Raises TvdbError."""
        query = " ".join(query.split())
        if not query:
            return []
        lang = self.config.language
        key = f"search:{kind}:{lang}:{query.lower()}"
        cached = self._cached(key, self.config.cache_ttl_days * 86400)
        if cached is not None:
            return parse_search(cached, lang)
        params = urllib.parse.urlencode({"query": query, "type": kind, "limit": 20})
        for attempt in range(2):
            status, payload = self.http(
                "GET", f"{BASE_URL}/search?{params}",
                {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                None,
            )
            if status == 401 and attempt == 0:
                self._forget("token")  # expired or revoked: log in again once
                continue
            break
        if status != 200:
            message = payload.get("message") or f"HTTP {status}"
            raise TvdbError(f"TVDB search failed ({message})")
        self._store(key, payload)
        return parse_search(payload, lang)
