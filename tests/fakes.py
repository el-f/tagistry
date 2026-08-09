"""Hermetic provider fakes for fixer golden tests (no network)."""

from __future__ import annotations

from collections.abc import Iterable

from tagistry.providers.acoustid import AcoustIDMatch
from tagistry.providers.musicbrainz import ArtistIdentity


class FakeMusicBrainz:
    def __init__(
        self,
        artists: dict[str, tuple[str, int]] | None = None,
        recordings: dict[tuple[str, str], int] | None = None,
        by_id: dict[str, tuple[str, str]] | None = None,
        tops: dict[str, tuple[str, str, int]] | None = None,
        tops_multi: dict[str, list[tuple[str, str, int]]] | None = None,
        identities: dict[str, ArtistIdentity] | None = None,
        years: dict[str, str] | None = None,
    ) -> None:
        self.artists = artists or {}
        self.recordings = recordings or {}
        self.by_id = by_id or {}
        self.tops = tops or {}
        self.tops_multi = tops_multi or {}
        self.identities = identities or {}
        self.years = years or {}

    def recording_year(self, mbid: str) -> str | None:
        return self.years.get(mbid)

    def artist_search(self, query: str) -> tuple[str, int]:
        return self.artists.get(query, ("", 0))

    def artist_identity(self, query: str) -> ArtistIdentity | None:
        return self.identities.get(query)

    def recording_search(self, title: str, artist: str) -> int:
        return self.recordings.get((title, artist), 0)

    def recording_by_id(self, mbid: str) -> tuple[str, str] | None:
        return self.by_id.get(mbid)

    def recording_top(self, title: str, artist: str = "") -> tuple[str, str, int] | None:
        return self.tops.get(title)

    def recording_tops(self, title: str, limit: int = 3) -> list[tuple[str, str, int]]:
        # a single "tops" entry is one match; "tops_multi" holds an explicit list for ambiguity
        if title in self.tops_multi:
            return self.tops_multi[title][:limit]  # honour limit: a narrow window hides disagreement
        one = self.tops.get(title)
        return [one] if one else []


class FakeResearcher:
    def __init__(self, answer: object) -> None:
        self._answer = answer

    def resolve(self, question: object) -> object:
        return self._answer


class FakeAcoustID:
    def __init__(self, match: AcoustIDMatch | None) -> None:
        self._match = match

    def identify(self, path: str) -> AcoustIDMatch | None:
        return self._match


class FakeDiscogs:
    """Maps (artist, title) to a curated genre list; unknown pairs return []."""

    def __init__(self, genres: dict[tuple[str, str], list[str]] | None = None) -> None:
        self._genres = genres or {}

    def genres(self, artist: str, title: str) -> list[str]:
        return self._genres.get((artist, title), [])


class FakeLastFm:
    """Maps an artist spelling to its last.fm listener count; unknown spellings return None."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def listeners(self, name: str) -> int | None:
        return self._counts.get(name)

    def scrobble_counts(self, names: Iterable[str]) -> dict[str, int]:
        return {n: self._counts[n] for n in names if n in self._counts}
