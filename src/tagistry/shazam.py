"""Two-fingerprinter agreement: does a second, independent fingerprinter confirm a retitle?

A single fingerprinter can tie between two recordings and pick the wrong one, silently rewriting
correct tags. So a fingerprint retitle is trusted ONLY when a second, independent fingerprinter
(Shazam) AGREEs with it, and this module owns that decision. `classify()` is pure (stdlib only)
so it is unit-tested here AND reused verbatim by `scripts/shazam_verify.py`, which runs
out-of-process on py3.12 because shazamio segfaults on 3.14. `read_verdicts()` loads that
script's output; `pipeline.shazam_filter` applies the verdicts to a review CSV.
"""

from __future__ import annotations

import csv
import difflib
import re
from dataclasses import dataclass

AGREE = "AGREE"
SAYS_PLAIN = "SAYS_PLAIN"
DIFFERENT = "DIFFERENT"
NO_MATCH = "NO_MATCH"

_TRAIL = re.compile(r"\s*[\(\[][^()\[\]]*[)\]]\s*$")
_FEAT = re.compile(r"\s*\b(?:feat|ft|featuring)\b.*$", re.IGNORECASE)
_MARKER = re.compile(
    r"\b(remix|revision|rework|edit|version|instrumental|acoustic|live|demo|bootleg|mashup|"
    r"unplugged|remaster|reprise|radio|session|cover|flip|dub|vip|mix)\b",
    re.IGNORECASE,
)


def _key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _base(t: str) -> str:
    """Strip every trailing (...) group and a feat clause, so a marker/feature diff is compared
    apart from the base title."""
    s = t or ""
    while (stripped := _TRAIL.sub("", s)) != s:  # one sub per pass, not two
        s = stripped
    return _key(_FEAT.sub("", s))


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _key(a), _key(b)).ratio()


def classify(current: str, proposed: str, shazam_title: str) -> str:
    """Verdict for a fingerprint proposal given Shazam's independent title.

    AGREE      : Shazam's title matches the proposed one -> two fingerprinters agree, trust it.
    SAYS_PLAIN : Shazam matches the current title, not the proposal -> the proposed marker/retitle
                 is unconfirmed; keep current.
    DIFFERENT  : Shazam names another song/version entirely -> a mismatch or a version neither
                 fingerprinter pinned; needs a human.
    NO_MATCH   : Shazam recognized nothing.
    """
    if not shazam_title:
        return NO_MATCH
    if _sim(shazam_title, proposed) > 0.85 or (
        _base(shazam_title) == _base(proposed) and bool(_MARKER.search(shazam_title)) == bool(_MARKER.search(proposed))
    ):
        return AGREE
    if _sim(shazam_title, current) > 0.85 or _base(shazam_title) == _base(current):
        return SAYS_PLAIN
    return DIFFERENT


@dataclass(frozen=True, slots=True)
class Verdict:
    """One file's Shazam result: the title-agreement verdict plus the artist Shazam heard. The
    artist is needed because `verdict` compares TITLES only -- an artist rewrite must ALSO be
    corroborated against `shazam_artist`, or a wrong-upload artist passes on a title match."""

    verdict: str
    shazam_artist: str = ""


def read_verdicts(csv_path: str) -> dict[str, Verdict]:
    """Load {file path -> Verdict} from a shazam_verify.py output CSV. A row missing path is
    skipped; the last row for a path wins (the script rewrites the whole file per run). A CSV
    without a shazam_artist column still loads (artist ''), so an old verdicts file works."""
    verdicts: dict[str, Verdict] = {}
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            path = (row.get("path") or "").strip()
            if path:
                verdicts[path] = Verdict((row.get("verdict") or "").strip(), (row.get("shazam_artist") or "").strip())
    return verdicts
