"""Offline self-consistency prior over the whole library.

An artist is "known" if it appears in >= 2 files (the library itself is the
ground truth) or was confirmed by MusicBrainz. This is what makes flip detection
precise without any network call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from ..domain import Track, is_probably_band, parse_credits
from ..text import key

# Only acts whose parts are not themselves library artists; stored match-normalized (text.key).
_KNOWN_SINGLE_ACTS = frozenset(
    key(n)
    for n in (
        "Earth, Wind & Fire",
        "Crosby, Stills, Nash & Young",
        "Crosby, Stills & Nash",
        "Tyler, The Creator",
        "Blood, Sweat & Tears",
        "Nico & Vinz",
    )
)


class LibraryPrior:
    def __init__(self, min_count: int = 2) -> None:
        self._counts: Counter[str] = Counter()
        self._known: set[str] = set()
        self._min = min_count

    @classmethod
    def from_tracks(cls, tracks: Iterable[Track], min_count: int = 2) -> LibraryPrior:
        lp = cls(min_count)
        for t in tracks:
            lp.add_artist(t.get("artist"))
        lp.finalize()
        return lp

    def add_artist(self, artist: str) -> None:
        if artist.strip():
            self._counts[key(artist)] += 1

    def add_verified(self, *names: str) -> None:
        """Mark a name as known regardless of frequency (e.g. MB-confirmed)."""
        for n in names:
            if n.strip():
                self._known.add(key(n))

    def finalize(self) -> None:
        self._known |= {k for k, c in self._counts.items() if c >= self._min}

    def is_known_artist(self, name: str) -> bool:
        return key(name) in self._known

    def title_is_known_artist(self, track: Track) -> bool:
        """The title field names a known artist (flip smell). Only a candidate — flip acts on it
        exclusively after MusicBrainz verifies the swap, so a common-word coincidence is caught there."""
        title = track.get("title")
        return bool(title.strip()) and self.is_known_artist(title)

    def is_single_act(self, artist: str) -> bool:
        """Offline: this flat artist string is ONE act to keep whole, not a list to split.

        True when it's a backing-band/vs pattern (is_probably_band), a curated famous band,
        or an 'A & B' / 'A, B' co-lead whose EVERY side is separately a known library artist.
        A feat/ft guest is never a single act (it belongs in the title), so it stays flag-worthy.
        This is the offline mirror of Checks.is_collaboration, without a network call."""
        if not artist.strip():
            return False
        if is_probably_band(artist) or key(artist) in _KNOWN_SINGLE_ACTS:
            return True
        credits = parse_credits(artist)
        if len(credits) < 2 or any(c.is_feat_join() for c in credits):
            return False
        return all(self.is_known_artist(c.credited_name) for c in credits)
