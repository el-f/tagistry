"""Pure domain models. No I/O, no network."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from enum import Enum

# Lives in this leaf module so the scan and gate passes both import it with no cycle
type ProgressFn = Callable[[int, int, str], None]

# Captured, so display() round-trips exactly; "/" and dash need spaces so "AC/DC" stays whole
_CREDIT_SEP = re.compile(
    r"(\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|\s+presents\s+|\s+pres\.?\s+"
    r"|\s*;\s*|\s*,\s*|\s+&\s+|\s+x\s+|\s+vs\.?\s+|\s+/\s+)",
    re.IGNORECASE,
)
_FEAT_JP = re.compile(r"feat|ft|featuring|presents|pres", re.IGNORECASE)
# A single credit whose "& The X" / "vs" is part of one band name, not a separator.
_BACKING = re.compile(r"&\s+(?:the|his|her|their)\s+|\bvs\.?\s+", re.IGNORECASE)


class Confidence(Enum):
    """How safe a proposal is. Only HIGH auto-applies."""

    HIGH = "HIGH"
    REVIEW = "REVIEW"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Why a fixer proposed a change."""

    source: str
    score: int
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ArtistCredit:
    """One entry in an ordered artist-credit list, mirroring MusicBrainz.

    The join phrase to the *next* entry carries the semantic signal:
    " feat. " = guest, " & " = collaboration, " / " = split. The last entry's
    join_phrase is "". A band whose name contains "&" is a single credit.
    """

    credited_name: str
    canonical_name: str | None = None
    mbid: str | None = None
    join_phrase: str = ""

    def is_feat_join(self) -> bool:
        return bool(_FEAT_JP.search(self.join_phrase))


def parse_credits(artist: str) -> list[ArtistCredit]:
    """Split a flat artist string into ordered credits, preserving separators."""
    if not artist:
        return []
    parts = _CREDIT_SEP.split(artist)
    credits: list[ArtistCredit] = []
    # parts alternate name, sep, name, sep, ..., name
    for i in range(0, len(parts), 2):
        name = parts[i]
        jp = parts[i + 1] if i + 1 < len(parts) else ""
        credits.append(ArtistCredit(credited_name=name, join_phrase=jp))
    return credits


def display_credits(credits: list[ArtistCredit]) -> str:
    """Rejoin credits into a flat string. Inverse of parse_credits."""
    return "".join(c.credited_name + c.join_phrase for c in credits)


def primary_credit(credits: list[ArtistCredit]) -> str:
    """The first credited name (the act to attribute a scrobble to)."""
    return credits[0].credited_name.strip() if credits else ""


def is_probably_band(name: str) -> bool:
    """Heuristic: the name reads as one band, not a joinable list."""
    return bool(_BACKING.search(name))


@dataclass(frozen=True, slots=True)
class Track:
    """A read audio file: path, extension, tags, and audio info."""

    path: str
    ext: str
    tags: dict[str, str]
    length: float = 0.0
    bitrate: int = 0
    codec: str = ""

    def get(self, field_name: str) -> str:
        return self.tags.get(field_name, "")


@dataclass(frozen=True, slots=True)
class Proposal:
    """A single proposed tag change, with confidence and evidence.

    file_artist/file_title carry the file's identity at scan time -- the row changes ONE field, but
    judging an identity change ('drop the co-artist?', 'is this the right song?') needs the other
    half. They travel to the review CSV as context columns so a row is reviewable on its own."""

    track_path: str
    field: str
    current: str
    proposed: str
    confidence: Confidence
    evidence: Evidence
    fixer: str
    file_artist: str = ""
    file_title: str = ""


def make_proposal(
    track: Track,
    field: str,
    current: str,
    proposed: str,
    confidence: Confidence,
    fixer: str,
    score: int,
    reason: str,
) -> Proposal:
    """Build a Proposal from the track, with its Evidence, in one place, so the field order lives
    once. Captures the file's artist/title as review context. Every producer (the fixers, the
    library planners) goes through here — a reorder of the Proposal fields can't break a caller."""
    return Proposal(
        track_path=track.path,
        field=field,
        current=current,
        proposed=proposed,
        confidence=confidence,
        evidence=Evidence(fixer, score, reason),
        fixer=fixer,
        file_artist=track.get("artist"),
        file_title=track.get("title"),
    )


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """One row of the editable review CSV: the decision plus the staged change.

    The read-side twin of a written Proposal. Typed access (row.field, not row['field'])
    lets mypy catch a wrong/renamed column at compile time instead of yielding '' deep in
    apply(). Frozen: a consumer that changes a decision returns a new row via replace()."""

    apply: str
    fixer: str
    confidence: str
    path: str
    field: str
    current: str
    proposed: str
    evidence: str
    file_artist: str = ""  # the file's identity at scan (context for reviewing the change);
    file_title: str = ""  # empty when reading an OLD CSV written before these columns existed

    @classmethod
    def from_dict(cls, d: Mapping[str, str]) -> ReviewRow:
        # `or ""`, not a get-default: DictReader's restval leaves a short row's columns None
        return cls(**{c: d.get(c) or "" for c in REVIEW_COLUMNS})

    def to_dict(self) -> dict[str, str]:
        return {c: getattr(self, c) for c in REVIEW_COLUMNS}

    @property
    def is_apply(self) -> bool:
        """The decision column reads 'apply' (case/space-insensitive)."""
        return self.apply.strip().lower() == "apply"


# The CSV column order, derived from the dataclass so the two never drift.
REVIEW_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(ReviewRow))
# The file_* columns are informational, so a CSV written before they existed still loads
REQUIRED_REVIEW_COLUMNS: tuple[str, ...] = tuple(c for c in REVIEW_COLUMNS if not c.startswith("file_"))


@dataclass(slots=True)
class ApplyResult:
    """Outcome of applying a review file."""

    applied: int = 0
    skipped: int = 0
    locked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class StaleChangeError(Exception):
    """The file no longer holds the value Tagistry wrote, so reverting would destroy a newer edit."""

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"changed since tagistry wrote it, not reverted: {path} ({detail})")
        self.path = path


class FileLockedError(Exception):
    """A player (or another process) holds the file open. Retried, then surfaced."""

    def __init__(self, path: str) -> None:
        super().__init__(f"file locked: {path}")
        self.path = path
