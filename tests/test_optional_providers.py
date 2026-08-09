"""last.fm + Discogs providers — parsing over an injected HTTP getter (hermetic)."""

from __future__ import annotations

from tagistry.providers.discogs import Discogs
from tagistry.providers.lastfm import LastFm


def test_lastfm_track_exists() -> None:
    hit = LastFm("k", lambda url: {"track": {"name": "Creep", "artist": {"name": "Radiohead"}}})
    assert hit.track_exists("Radiohead", "Creep")
    miss = LastFm("k", lambda url: {"error": 6, "message": "Track not found"})
    assert not miss.track_exists("Nobody", "Nothing")


def test_discogs_genres_styles_first() -> None:
    def getter(url: str) -> dict[str, object] | None:
        assert "type=release" in url and "token=tok" in url
        return {"results": [{"genre": ["Electronic"], "style": ["House", "Techno"]}]}

    assert Discogs("tok", getter).genres("Daft Punk", "One More Time") == ["House", "Techno", "Electronic"]


def test_discogs_no_results_is_empty() -> None:
    assert Discogs("tok", lambda url: {"results": []}).genres("x", "y") == []
    assert Discogs("tok", lambda url: None).genres("x", "y") == []
