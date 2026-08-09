"""MusicBrainz ws/2 JSON provider. Cached (SQLite) + rate-limited (1 req/s).

Direct ws/2 JSON, not musicbrainzngs (abandoned, XML-only). The session is
injectable so tests can drive it with a plain session under vcrpy cassettes.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from requests import Session
from requests.exceptions import RequestException

from .. import config
from .http import USER_AGENT, cached_limited_session

if TYPE_CHECKING:
    from requests import Response

_BASE = "https://musicbrainz.org/ws/2"
_RETRIES = 4
_BACKOFF = 1.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class ArtistIdentity:
    """One MusicBrainz artist: its MBID, primary name, and alias display names. Aliases belong to
    this one MBID, so name + aliases are all real spellings of the SAME artist."""

    mbid: str
    name: str
    score: int
    aliases: tuple[str, ...]

    def spellings(self) -> tuple[str, ...]:
        """Primary name + aliases, de-duplicated (order preserved)."""
        return tuple(dict.fromkeys((self.name, *self.aliases)))


def _backoff(attempt: int) -> float:
    """Linear backoff with full jitter, so N parallel scans don't retry in lockstep and re-trip
    the same rate limit (tagio does the same for its file-lock retries)."""
    return _BACKOFF * (attempt + 1) + random.uniform(0, _BACKOFF)  # noqa: S311 -- retry jitter, not crypto


def _phrase(s: str) -> str:
    """Escape a value for a quoted Lucene phrase (recording:"..."): a raw '"' or '\\' in a title
    ('Say "Hello"', a path with a backslash) would break the query and MB answers 400."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _default_session(cache_path: Path) -> Session:
    return cached_limited_session(cache_path, per_second=1)  # MusicBrainz allows 1 req/s


class MusicBrainz:
    def __init__(self, session: Session | None = None, cache_path: Path | None = None) -> None:
        if session is None:
            path = cache_path or Path(config.cache_path("mb_cache"))
            path.parent.mkdir(parents=True, exist_ok=True)
            session = _default_session(path)
        self._session = session

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        url = f"{_BASE}/{endpoint}"
        headers = {"User-Agent": USER_AGENT}
        query = {**params, "fmt": "json"}
        for attempt in range(_RETRIES):
            try:
                resp: Response = self._session.get(url, params=query, headers=headers, timeout=20)
                if resp.status_code in _RETRYABLE_STATUS and attempt < _RETRIES - 1:
                    time.sleep(_backoff(attempt))  # MB is briefly overloaded — back off (jittered)
                    continue
                resp.raise_for_status()
                data: dict[str, object] = resp.json()
                return data
            except RequestException:
                if attempt < _RETRIES - 1:
                    time.sleep(_backoff(attempt))
                    continue
                raise
        return {}

    def artist_search(self, query: str) -> tuple[str, int]:
        """Top artist match: (name, score 0-100). Empty on an unexpected response."""
        data = self._get("artist", {"query": query, "limit": "3"})
        artists = data.get("artists")
        if not isinstance(artists, list) or not artists or not isinstance(artists[0], dict):
            return "", 0
        top = artists[0]
        return str(top.get("name", "")), int(top.get("score", 0) or 0)

    def artist_identity(self, query: str) -> ArtistIdentity | None:
        """Top artist match as an ArtistIdentity (MBID, name, score, alias display names), or None.

        Aliases come from a second lookup (`inc=aliases`) so they are complete and typed; only
        real name aliases are kept (a 'Legal name'/'Search hint' is not a display spelling). This
        does NOT gate on the score -- the caller decides whether the match is trustworthy (the
        aliases are only same-artist if the top hit is actually this artist)."""
        data = self._get("artist", {"query": query, "limit": "3"})
        artists = data.get("artists")
        if not isinstance(artists, list) or not artists or not isinstance(artists[0], dict):
            return None
        top = artists[0]
        mbid, name = str(top.get("id", "")), str(top.get("name", ""))
        score = int(top.get("score", 0) or 0)
        if not mbid:
            return None
        aliases = self._artist_aliases(mbid)
        return ArtistIdentity(mbid, name, score, aliases)

    def _artist_aliases(self, mbid: str) -> tuple[str, ...]:
        data = self._get(f"artist/{mbid}", {"inc": "aliases"})
        raw = data.get("aliases")
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            # A typed 'Legal name' / 'Search hint' is not a display spelling, so skip those
            if a.get("type") in (None, "Artist name") and isinstance(a.get("name"), str) and a["name"].strip():
                names.append(str(a["name"]))
        return tuple(dict.fromkeys(names))

    def recording_search(self, title: str, artist: str) -> int:
        """Best recording score for (title by artist), 0-100. 0 on an unexpected response."""
        query = f'recording:"{_phrase(title)}" AND artist:"{_phrase(artist)}"'
        data = self._get("recording", {"query": query, "limit": "1"})
        recordings = data.get("recordings")
        if not isinstance(recordings, list) or not recordings or not isinstance(recordings[0], dict):
            return 0
        return int(recordings[0].get("score", 0) or 0)

    def recording_tops(self, title: str, limit: int = 3) -> list[tuple[str, str, int]]:
        """Top recordings for a title: [(title, artist-credit, score)]. Used to check that
        the matches agree on an artist before trusting a title -> artist lookup."""
        data = self._get("recording", {"query": f'recording:"{_phrase(title)}"', "limit": str(limit)})
        recs = data.get("recordings")
        if not isinstance(recs, list):
            return []
        out: list[tuple[str, str, int]] = []
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            credits = rec.get("artist-credit") or []
            ac = "".join(str(c.get("name", "")) + str(c.get("joinphrase", "")) for c in credits if isinstance(c, dict))
            out.append((str(rec.get("title", "")), ac, int(rec.get("score", 0) or 0)))
        return out

    def recording_top(self, title: str, artist: str = "") -> tuple[str, str, int] | None:
        """Top recording for a title (optionally by artist): (title, artist-credit, score)."""
        query = f'recording:"{_phrase(title)}"' + (f' AND artist:"{_phrase(artist)}"' if artist else "")
        data = self._get("recording", {"query": query, "limit": "1"})
        recs = data.get("recordings")
        if not isinstance(recs, list) or not recs or not isinstance(recs[0], dict):
            return None
        rec = recs[0]
        credits = rec.get("artist-credit") or []
        ac = "".join(str(c.get("name", "")) + str(c.get("joinphrase", "")) for c in credits if isinstance(c, dict))
        return str(rec.get("title", "")), ac, int(rec.get("score", 0) or 0)

    def recording_by_id(self, mbid: str) -> tuple[str, str] | None:
        """Canonical (title, artist-credit) for a recording MBID, or None.

        The artist-credit joins names with their join phrases, so a feat/collab renders
        the MusicBrainz way: 'JAY-Z & Kanye West', 'The Weeknd feat. Ariana Grande'.
        """
        data = self._get(f"recording/{mbid}", {"inc": "artist-credits"})
        title = data.get("title")
        if not isinstance(title, str) or not title:
            return None
        credits = data.get("artist-credit")
        if not isinstance(credits, list):
            return title, ""
        artist = "".join(str(c.get("name", "")) + str(c.get("joinphrase", "")) for c in credits if isinstance(c, dict))
        return title, artist

    def recording_year(self, mbid: str) -> str | None:
        """Earliest release year for a recording MBID (its first appearance), or None. The earliest
        of the recording's releases is the original year -- a later comp/reissue shouldn't win."""
        data = self._get(f"recording/{mbid}", {"inc": "releases"})
        releases = data.get("releases")
        if not isinstance(releases, list):
            return None
        years: list[int] = []
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            date = rel.get("date")
            if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
                years.append(int(date[:4]))
        return str(min(years)) if years else None
