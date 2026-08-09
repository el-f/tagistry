"""Discogs provider: curated genres/styles for an (artist, title). Optional, needs a token.

Discogs genres are hand-curated (unlike free-text last.fm tags), which makes them a good source
for a future genre-fill. The HTTP getter is injected so tests stay hermetic.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

type JsonGetter = Callable[[str], dict[str, object] | None]

_SEARCH = "https://api.discogs.com/database/search"


class Discogs:
    def __init__(self, token: str, json_getter: JsonGetter) -> None:
        self._token = token
        self._get = json_getter

    def genres(self, artist: str, title: str) -> list[str]:
        """Genres + styles for the best release match, most-specific (styles) first. Empty if none."""
        query = urlencode({"token": self._token, "type": "release", "artist": artist, "track": title, "per_page": "1"})
        data = self._get(f"{_SEARCH}?{query}")
        if not data:
            return []
        results = data.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return []
        top = results[0]
        styles = [str(s) for s in (top.get("style") or []) if str(s).strip()]
        genres = [str(g) for g in (top.get("genre") or []) if str(g).strip()]
        # styles are the finer label (House); genres the broad one (Electronic). De-dupe, styles first.
        return list(dict.fromkeys(styles + genres))


def default_discogs(token: str, cache_path: str | None = None, timeout: int = 15) -> Discogs:
    # Discogs caps unauthenticated callers at ~60 req/min; 1 req/s stays under it, cached + reused.
    from .. import config
    from .http import USER_AGENT, cached_limited_session, json_getter

    session = cached_limited_session(cache_path or config.cache_path("discogs_cache"), per_second=1)
    return Discogs(token, json_getter(session, timeout, headers={"User-Agent": USER_AGENT}))
