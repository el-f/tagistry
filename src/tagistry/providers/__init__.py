"""Ground-truth providers, cached and rate-limited.

A Providers bundle is passed to fixers. Any provider may be None (offline run);
fixers that need one check for it and emit a REVIEW proposal when it is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..research import NullResearcher, Researcher
from .acoustid import AcoustID
from .discogs import Discogs
from .lastfm import LastFm, LastFmPage, ScrobbleSource
from .library_prior import LibraryPrior
from .musicbrainz import MusicBrainz


class CoverFetcher(Protocol):
    """A source of front-cover image bytes for a track. Returns (bytes, source-name) or None.
    The contract lives at the boundary so covers.py depends on this, not a concrete fetcher."""

    def fetch(self, artist: str, title: str, release_mbid: str | None = None) -> tuple[bytes, str] | None: ...


@dataclass(slots=True)
class Providers:
    library: LibraryPrior | None = None
    musicbrainz: MusicBrainz | None = None
    acoustid: AcoustID | None = None
    lastfm: ScrobbleSource | None = None  # API-backed LastFm, or keyless LastFmPage
    discogs: Discogs | None = None  # curated genres, on a $DISCOGS_TOKEN
    researcher: Researcher = field(default_factory=NullResearcher)


__all__ = [
    "AcoustID",
    "CoverFetcher",
    "Discogs",
    "LastFm",
    "LastFmPage",
    "LibraryPrior",
    "MusicBrainz",
    "NullResearcher",
    "Providers",
    "Researcher",
    "ScrobbleSource",
]
