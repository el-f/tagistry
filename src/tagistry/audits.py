"""Read-only audits over the library: anomaly report and duplicate finder. Pure over already-read
tracks where possible; the two that scan the library (audit_library, find_duplicates) read through
tagio.read_tracks. These never produce proposals -- the mutating library planners live in planners.py.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .domain import ProgressFn, Track
from .providers import LibraryPrior
from .tagio import read_tracks
from .text import key

# Codec quality tiers, best first: lossless > opus > aac > lossy. Used to pick the keep-file.
_CODEC_TIER = {"flac": 4, "alac": 4, "opus": 3, "aac": 2, "m4a": 2, "mp3": 1, "vorbis": 1, "ogg": 1}
_MULTI_AUDIT = re.compile(r"[;,]|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b", re.IGNORECASE)


def _quality(track: Track) -> tuple[int, int]:
    tier = _CODEC_TIER.get(track.codec.lower()) or _CODEC_TIER.get(track.ext.lower(), 0)
    return tier, track.bitrate


def find_duplicates(root: str, progress: ProgressFn | None = None) -> list[list[Track]]:
    """Files that share an artist+title (case/`&`-insensitive), best-quality first in each group.
    Metadata-level dedup; the first entry is the keep suggestion (highest codec tier + bitrate)."""
    groups: dict[tuple[str, str], list[Track]] = defaultdict(list)
    for track in read_tracks(root, progress):
        artist, title = track.get("artist").strip(), track.get("title").strip()
        if artist and title:
            groups[(key(artist), key(title))].append(track)
    dups = [sorted(g, key=_quality, reverse=True) for g in groups.values() if len(g) > 1]
    return sorted(dups, key=lambda g: (g[0].get("artist").lower(), g[0].get("title").lower()))


# A blank file this long, alone in its folder, is a mix or full-OST rip -- no single recording to tag
_MIX_MIN_SECONDS = 20 * 60
MIX_ISSUE = "long mix/compilation (no single recording)"


def _is_long_mix(track: Track, folder_count: int) -> bool:
    return track.length >= _MIX_MIN_SECONDS and folder_count <= 2


def audit_tracks(tracks: list[Track]) -> list[tuple[str, str]]:
    """Pure anomaly report over already-read tracks (no I/O): blank artist/title,
    artist==title[==album], multi-artist in the artist field.

    A blank-field file that is hours long and alone in its folder is relabeled a mix/compilation
    (an irreducible case), not counted as a blank anomaly. A co-lead of two artists both known in
    this library ('A & B', 'A, B') is NOT a multi-artist problem; a feat/ft guest still is."""
    prior = LibraryPrior.from_tracks(tracks)
    folder_counts = Counter(str(Path(t.path).parent) for t in tracks)
    issues: list[tuple[str, str]] = []
    for track in tracks:
        artist, title, album = track.get("artist").strip(), track.get("title").strip(), track.get("album").strip()
        if (not artist or not title) and _is_long_mix(track, folder_counts[str(Path(track.path).parent)]):
            issues.append((track.path, MIX_ISSUE))
            continue
        if not artist:
            issues.append((track.path, "blank artist"))
        if not title:
            issues.append((track.path, "blank title"))
        if artist and title and key(artist) == key(title):
            both = artist and album and key(album) == key(artist)
            issues.append((track.path, "artist == title == album" if both else "artist == title"))
        elif artist and _MULTI_AUDIT.search(artist) and not prior.is_single_act(artist):
            issues.append((track.path, "multi-artist in artist field"))
    return issues


def audit_library(root: str, progress: ProgressFn | None = None) -> list[tuple[str, str]]:
    """Read the library and run the anomaly report — the 'is my collection clean?' check."""
    return audit_tracks(read_tracks(root, progress))
