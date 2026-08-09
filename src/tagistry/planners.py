"""Whole-library proposal planners: `(tracks, providers) -> [Proposal]`.

These stage tag changes like the fixers do, but they are NOT per-track fixers -- each needs
context a single Track can't carry: the folder's dominant artist (albumartist), or one resolve
per distinct artist across the whole library (scrobble names). So they take the full track list,
run outside the fixer registry, and are staged straight to a review CSV. Both emit REVIEW only.

The per-track fixers live in fixers.py (registered, run by pipeline.scan with dedup + resume);
these are the second tier. audits.py is the read-only sibling (reports, never proposals).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from .checks import ARTIST_MATCH_MIN, Checks
from .domain import Confidence, Proposal, Track, make_proposal
from .providers import Providers
from .scrobble import pick_scrobble_name
from .text import key

# Below this the folder is a compilation, where albumartist should be 'Various Artists'
_ALBUMARTIST_SHARE_MIN = 0.8


def plan_albumartist(tracks: Iterable[Track]) -> list[Proposal]:
    """Fill a BLANK albumartist from the folder's dominant artist, but only when the folder is
    clearly one artist's album (>= 80% share). A blank albumartist splits an album across players
    that group by it. Compilations (mixed artists) are left alone -- 'Various Artists' is a
    separate decision. REVIEW: albumartist drives grouping, so a human confirms."""
    by_folder: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        by_folder[str(Path(t.path).parent)].append(t)
    proposals: list[Proposal] = []
    for ts in by_folder.values():
        named = [t for t in ts if t.get("artist").strip()]
        if not named:
            continue
        counts = Counter(key(t.get("artist")) for t in named)
        dom_key, dom_count = counts.most_common(1)[0]
        if dom_count / len(ts) < _ALBUMARTIST_SHARE_MIN:
            continue  # compilation / mixed folder -> skip
        dominant = next(t.get("artist") for t in named if key(t.get("artist")) == dom_key)
        for t in ts:
            if not t.get("albumartist").strip():  # only fill blanks, never overwrite an existing one
                reason = f"folder is one artist ({dom_count}/{len(ts)})"
                proposals.append(
                    make_proposal(t, "albumartist", "", dominant, Confidence.REVIEW, "albumartist", 90, reason)
                )
    return proposals


# --- scrobble-name canonicalization -----------------------------------------

_SCROBBLE_MARGIN = 1.25  # switch spelling only when the winner has >= 1.25x the current's listeners


def plan_scrobble_names(
    tracks: Iterable[Track], providers: Providers, margin: float = _SCROBBLE_MARGIN
) -> list[Proposal]:
    """Retag each artist to the spelling scrobbles land on: the most-scrobbled last.fm form among
    that artist's MusicBrainz aliases (a transliteration, a script variant, a rename like Kanye
    West -> Ye). Resolves ONCE per distinct artist -- many tracks share one, and all get the same
    answer. REVIEW: a name rewrite always goes through review. Dormant without a last.fm key + MB."""
    lf, mb = providers.lastfm, providers.musicbrainz
    if lf is None or mb is None:
        return []
    checks = Checks(mb)
    by_artist: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        name = t.get("artist").strip()
        if name:
            by_artist[name].append(t)
    proposals: list[Proposal] = []
    for artist, ts in by_artist.items():
        if checks.is_collaboration(artist):
            continue  # a collab, not one artist to canonicalize
        identity = mb.artist_identity(artist)
        if identity is None or identity.score < ARTIST_MATCH_MIN:
            continue
        spellings = identity.spellings()
        if key(artist) not in {key(s) for s in spellings}:
            continue  # the MB hit is not confidently this tagged artist -> don't trust its aliases
        # The current tag is a candidate too: its own count is the baseline the aliases must beat
        counts = lf.scrobble_counts((artist, *spellings))
        choice = pick_scrobble_name(artist, counts, margin)
        if not choice.changed:
            continue
        proposals.extend(
            make_proposal(t, "artist", artist, choice.name, Confidence.REVIEW, "scrobble_name", 90, choice.reason)
            for t in ts
        )
    return proposals
