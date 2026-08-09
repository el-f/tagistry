"""Shared HTTP session: cached (SQLite) + rate-limited, per requests-cache/requests-ratelimiter.

One factory so every provider's real (non-injected) path gets caching, rate-limiting, and a
timeout instead of a bare requests.get. Discogs enforces ~60 req/min, last.fm asks callers to cap
their rate, MusicBrainz allows 1 req/s, and re-running a scan should reuse the prior answers.
The concrete network calls stay behind the providers' injected getters, so tests never touch this.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from requests import Session
from requests_cache import CacheMixin
from requests_ratelimiter import LimiterMixin

from .. import __version__

# MusicBrainz blocks anonymous callers: its terms require app name, version and a contact URL.
USER_AGENT = f"Tagistry/{__version__} (+https://github.com/el-f/tagistry)"

_EXPIRE_SECONDS = 60 * 60 * 24 * 30  # 30 days: metadata answers are stable enough to reuse


class CachedLimiterSession(CacheMixin, LimiterMixin, Session):
    """requests-cache + requests-ratelimiter combined, per their docs."""


def cached_limited_session(cache_path: str | Path, per_second: float, expire_after: int = _EXPIRE_SECONDS) -> Session:
    """A Session that caches responses to a SQLite file and caps outgoing requests to per_second."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return CachedLimiterSession(
        cache_name=str(path),
        backend="sqlite",
        per_second=per_second,
        expire_after=expire_after,
        # never persist a secret to the on-disk cache: strip API keys/tokens from the stored request
        ignored_parameters=["api_key", "apikey", "token", "client", "X-Plex-Token"],
    )


def json_getter(
    session: Session, timeout: int, headers: dict[str, str] | None = None
) -> Callable[[str], dict[str, object] | None]:
    """A url -> parsed-JSON-or-None getter over a session (200 only, network/JSON errors swallowed
    to None). The provider decides what None means; a terminal auth error is the provider's to raise."""
    import requests

    def get(url: str) -> dict[str, object] | None:
        try:
            resp = session.get(url, timeout=timeout, headers=headers or {})
            if resp.status_code != 200:
                return None
            data: dict[str, object] = resp.json()
            return data
        except (requests.RequestException, ValueError):
            return None

    return get


def text_getter(session: Session, timeout: int, headers: dict[str, str] | None = None) -> Callable[[str], str | None]:
    """A url -> response-text-or-None getter (200 only, network errors swallowed). For scraping a
    public HTML page (e.g. the keyless last.fm artist page) when no JSON API/key is available."""
    import requests

    def get(url: str) -> str | None:
        try:
            resp = session.get(url, timeout=timeout, headers=headers or {})
            return resp.text if resp.status_code == 200 else None
        except requests.RequestException:
            return None

    return get
