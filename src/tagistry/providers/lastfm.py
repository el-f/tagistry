"""last.fm provider: artist-name correction + scrobble counts, for scrobble-name checks.

A tag that doesn't match last.fm's spelling orphans the scrobble (the original driver). The
canonical tag is the spelling with the most listeners. Two sources of that count, same interface:
LastFm uses the JSON API (`artist.getInfo`, needs a free key), LastFmPage scrapes the
public artist page (keyless, works out-of-the-box, but brittle to a last.fm HTML change). Both
subclass ScrobbleSource, which owns the per-spelling loop. HTTP getters are injected for tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import quote, urlencode

type JsonGetter = Callable[[str], dict[str, object] | None]
type TextGetter = Callable[[str], str | None]

_BASE = "https://ws.audioscrobbler.com/2.0/"
_PAGE_BASE = "https://www.last.fm/music/"
# The page renders two of these; the FIRST is listeners, the second is scrobbles
_PAGE_COUNT = re.compile(r'intabbr js-abbreviated-counter" title="([0-9,]+)"')


class ScrobbleSource:
    """A source of last.fm listener counts per artist spelling. Subclasses implement listeners()."""

    def listeners(self, name: str) -> int | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def scrobble_counts(self, names: Iterable[str]) -> dict[str, int]:
        """{spelling: last.fm listeners} for the spellings last.fm knows; unknown ones are omitted.
        Feed this to scrobble.pick_scrobble_name to choose the canonical tag."""
        out: dict[str, int] = {}
        for name in dict.fromkeys(n for n in names if n and n.strip()):  # dedup, preserve order
            count = self.listeners(name)
            if count is not None:
                out[name] = count
        return out


class LastFm(ScrobbleSource):
    """API-backed source (needs a free last.fm key). Also exposes track-existence."""

    def __init__(self, api_key: str, json_getter: JsonGetter) -> None:
        self._key = api_key
        self._get = json_getter

    def _url(self, method: str, **params: str) -> str:
        return _BASE + "?" + urlencode({"method": method, "api_key": self._key, "format": "json", **params})

    def listeners(self, name: str) -> int | None:
        """last.fm listener count for this EXACT artist spelling, or None if last.fm has no data.
        autocorrect=0 so each spelling reports its own page -- that is what lets a caller see which
        spelling scrobbles actually land on."""
        data = self._get(self._url("artist.getInfo", artist=name, autocorrect="0"))
        if not data:
            return None
        artist = data.get("artist")
        if not isinstance(artist, dict):
            return None
        stats = artist.get("stats")
        if not isinstance(stats, dict):
            return None
        raw = stats.get("listeners")  # last.fm returns counts as strings
        if not isinstance(raw, (str, int)):
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def track_exists(self, artist: str, title: str) -> bool:
        """True when last.fm knows this (artist, title) — i.e. a scrobble would match."""
        data = self._get(self._url("track.getInfo", artist=artist, track=title))
        if not data:
            return False
        return isinstance(data.get("track"), dict)


class LastFmPage(ScrobbleSource):
    """Keyless source: reads the listener count off the public artist page's HTML header. No key
    needed, so scrobble-names runs out-of-the-box -- but it depends on last.fm's page markup, so it
    is brittle by nature (a redesign breaks the regex). The API path (LastFm) is preferred when a
    key is present. The scrape is the no-key fallback; adding a key upgrades it to the JSON API."""

    def __init__(self, text_getter: TextGetter) -> None:
        self._get = text_getter

    def listeners(self, name: str) -> int | None:
        html = self._get(_PAGE_BASE + quote(name, safe=""))
        if not html:
            return None
        m = _PAGE_COUNT.search(html)  # first counter on the page = the artist's listeners
        return int(m.group(1).replace(",", "")) if m else None


def default_lastfm(api_key: str, cache_path: str | None = None, timeout: int = 15) -> LastFm:
    # last.fm asks callers to cap their rate; a few req/s + caching is plenty and re-run-cheap.
    from .. import config
    from .http import cached_limited_session, json_getter

    session = cached_limited_session(cache_path or config.cache_path("lastfm_cache"), per_second=5)
    return LastFm(api_key, json_getter(session, timeout))


def default_lastfm_page(cache_path: str | None = None, timeout: int = 15) -> LastFmPage:
    """Keyless page-scrape source with a browser User-Agent, cached + rate-limited like the API one."""
    from .. import config
    from .http import USER_AGENT, cached_limited_session, text_getter

    session = cached_limited_session(cache_path or config.cache_path("lastfm_page_cache"), per_second=2)
    headers = {"User-Agent": USER_AGENT}  # identify honestly; never spoof a browser
    return LastFmPage(text_getter(session, timeout, headers))
