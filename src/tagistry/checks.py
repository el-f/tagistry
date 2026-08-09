"""Layer-1 hardened online checks. Deterministic, cached (via the MB session), testable.

The one rule that gives good results: never trust a MusicBrainz score alone — require a
name match. MB returns score=100 for fuzzy junk ("Lilo & Stitch" -> "Lilo Lilo",
non-Latin -> unrelated act), so a bare score gate is a lie detector that always says true.
"""

from __future__ import annotations

import re

from .providers.musicbrainz import MusicBrainz
from .text import alnum, key, subset, tokens

# Spaces around '&'/'x' keep "W&W" and "Charli XCX" intact; feat/ft is feat_to_title's job, not a co-lead.
_COLLAB_SEP = re.compile(r"\s*,\s*|\s*/\s*|\s+&\s+|\s*×\s*|\s+and\s+|\s+x\s+|\s+vs\.?\s+|\s+versus\s+", re.IGNORECASE)

# MB scores fuzzy junk at 100, so every gate also requires a name match; these are only floors
ARTIST_MATCH_MIN = 90  # trust an artist-search hit
RECORDING_MATCH_MIN = 95  # stricter: a title -> artist lookup needs near-exact recording agreement
# At 3, the exact-title filter can leave one survivor and the agreement test passes on nothing.
_TITLE_CANDIDATES = 10


class Checks:
    def __init__(self, mb: MusicBrainz) -> None:
        self._mb = mb

    def is_real_artist(self, name: str) -> bool:
        """MB knows this artist AND the returned name actually matches (not just a high score)."""
        if not name.strip():
            return False
        found, score = self._mb.artist_search(name)
        if score < ARTIST_MATCH_MIN:
            return False
        if alnum(found) == alnum(name) or subset(name, found):
            return True
        # The tag may be the LONGER form; two tokens minimum, else "Lilo & Stitch" -> "Lilo Lilo" passes.
        return len(tokens(found)) > 1 and subset(found, name)

    def artist_for_title(self, title: str) -> str | None:
        """The canonical artist for a title — ONLY when the strong matches AGREE on one artist.

        A junk artist with a distinctive title ('He Mele No Lilo' -> 'Mark Keali'i Ho'omalu')
        resolves; a common title ('Eye In The Sky', many different artists) does not — it stays
        uncertain rather than guessing a wrong recording. That ambiguity is layer-2 residue."""
        if not title.strip():
            return None
        strong = [
            (t, a)
            for (t, a, s) in self._mb.recording_tops(title, _TITLE_CANDIDATES)
            if s >= RECORDING_MATCH_MIN and a and alnum(t) == alnum(title)
        ]
        if not strong:
            return None
        if len({key(a) for (_, a) in strong}) == 1:  # all matches agree on the artist
            return strong[0][1]
        return None  # ambiguous -> don't guess

    def is_collaboration(self, artist: str) -> bool:
        """Two or more real, distinct lead artists joined by a co-lead separator (&, x, vs, ',',
        '/', 'and', '×') — a collaboration to keep joint (The Weeknd & Ariana Grande; Jay-Z &
        Kanye West; Vanic x K.Flay; benny blanco, Halsey & Khalid). EVERY part must be a real MB
        artist, so a backing band ("& His Orchestra") fails and is left to split. feat/ft/pres. is
        not a co-lead separator — that guest belongs in the title, not the joint credit."""
        parts = [p.strip() for p in _COLLAB_SEP.split(artist) if p.strip()]
        if len(parts) < 2:
            return False
        return all(self.is_real_artist(p) for p in parts)
