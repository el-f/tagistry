"""Cover art fetch: Cover Art Archive (by release MBID) -> iTunes (by artist+title).

The Cover Art Archive is authoritative when we have a MusicBrainz release MBID; iTunes is the
no-MBID fallback that just needs the artist and title (the common case for a loose file). Both
HTTP callables are injected so the suite stays hermetic. Returned bytes are embedded as-is;
mediafile derives the mime type from the magic bytes.
"""

from __future__ import annotations

from collections.abc import Callable

type ImageGetter = Callable[[str], bytes | None]  # url -> image bytes, or None on miss/error
type JsonGetter = Callable[[str], dict[str, object] | None]  # url -> parsed JSON, or None

_CAA = "https://coverartarchive.org"
_ITUNES = "https://itunes.apple.com/search"


def _looks_like_image(data: bytes) -> bool:
    """True if the bytes start with a known image signature. Guards against a 200 error/HTML
    page (a rate-limit notice, a CDN placeholder) being embedded as cover art."""
    return (
        data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"BM"))  # JPEG / PNG / BMP
        or data[:6] in (b"GIF87a", b"GIF89a")  # GIF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WEBP
    )


class CoverArtArchive:
    def __init__(self, image_getter: ImageGetter) -> None:
        self._get = image_getter

    def front(self, mbid: str, kind: str = "release") -> bytes | None:
        """Front cover for a release / release-group MBID (500px), or None if none is on file."""
        if not mbid:
            return None
        return self._get(f"{_CAA}/{kind}/{mbid}/front-500")


class ITunes:
    def __init__(self, json_getter: JsonGetter, image_getter: ImageGetter) -> None:
        self._json = json_getter
        self._img = image_getter

    def front(self, artist: str, title: str) -> bytes | None:
        """Best song-artwork match for 'artist title', upscaled from the 100px thumb to 600px."""
        from urllib.parse import urlencode

        query = urlencode({"term": f"{artist} {title}", "entity": "song", "limit": "1"})
        data = self._json(f"{_ITUNES}?{query}")
        if not data:
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        thumb = results[0].get("artworkUrl100")
        if not isinstance(thumb, str) or not thumb:
            return None
        return self._img(thumb.replace("100x100", "600x600"))


class CoverArtFetcher:
    """Cascade: Cover Art Archive first (if a release MBID is known), then iTunes."""

    def __init__(self, caa: CoverArtArchive, itunes: ITunes) -> None:
        self._caa = caa
        self._itunes = itunes

    def fetch(self, artist: str, title: str, release_mbid: str | None = None) -> tuple[bytes, str] | None:
        if release_mbid:
            data = self._caa.front(release_mbid)
            if data:
                return data, "coverartarchive"
        data = self._itunes.front(artist, title)
        if data:
            return data, "itunes"
        return None


def default_fetcher(cache_path: str | None = None, timeout: int = 20) -> CoverArtFetcher:
    """The real fetcher over a cached + rate-limited session (used by the CLI)."""
    import requests

    from .. import config
    from .http import cached_limited_session
    from .http import json_getter as _json_getter

    session = cached_limited_session(cache_path or config.cache_path("coverart_cache"), per_second=5)

    def image_getter(url: str) -> bytes | None:
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            data = resp.content
            return data if resp.status_code == 200 and _looks_like_image(data) else None
        except requests.RequestException:
            return None

    return CoverArtFetcher(CoverArtArchive(image_getter), ITunes(_json_getter(session, timeout), image_getter))
