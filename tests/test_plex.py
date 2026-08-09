"""Plex refresh sink — parse sections, refresh music ones (hermetic, injected HTTP)."""

from __future__ import annotations

from tagistry.providers.plex import Plex

_SECTIONS: dict[str, object] = {
    "MediaContainer": {
        "Directory": [
            {"key": "1", "type": "artist", "title": "Music"},
            {"key": "2", "type": "movie", "title": "Movies"},
            {"key": "3", "type": "artist", "title": "Soundtracks"},
        ]
    }
}


def test_music_sections_filters_to_artist_type() -> None:
    plex = Plex("http://pms:32400", "tok", lambda url: _SECTIONS, lambda url: True)
    assert plex.music_sections() == ["1", "3"]


def test_refresh_music_hits_each_section_with_token() -> None:
    hits: list[str] = []

    def hitter(url: str) -> bool:
        hits.append(url)
        return True

    plex = Plex("http://pms:32400", "tok", lambda url: _SECTIONS, hitter)
    assert plex.refresh_music() == 2
    assert all("X-Plex-Token=tok" in u and "/refresh" in u for u in hits)
    assert "/library/sections/1/refresh" in hits[0]


def test_no_sections_when_unreachable() -> None:
    plex = Plex("http://pms:32400", "tok", lambda url: None, lambda url: False)
    assert plex.music_sections() == [] and plex.refresh_music() == 0
