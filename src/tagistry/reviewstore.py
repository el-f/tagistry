"""The review CSV store: the one owner of the staged-proposal on-disk format.

Every producer writes here and every gate reads here, so the row shape lives in exactly one
place. Writes are atomic (temp + os.replace) so a crash mid-write can't truncate the staged
state; the resumable-scan append is the one deliberate non-atomic write (it IS the resume net).
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterable
from typing import TextIO

from .atomicio import atomic_write, ensure_parent
from .domain import REQUIRED_REVIEW_COLUMNS, REVIEW_COLUMNS, Confidence, Proposal, ReviewRow

REVIEW_HEADER = list(REVIEW_COLUMNS)


# A cell starting with one of these is a FORMULA to Excel/LibreOffice, and tag text is untrusted
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _quote_formula(cell: str) -> str:
    return "'" + cell if cell.startswith(_FORMULA_LEAD) else cell


def _unquote_formula(cell: str) -> str:
    """Undo _quote_formula, leaving a real leading apostrophe ("'Round Midnight") alone."""
    return cell[1:] if cell[:1] == "'" and cell[1:2] in _FORMULA_LEAD else cell


def _proposal_row(p: Proposal) -> list[str]:
    """A Proposal as a review CSV row. One source for the row shape (write + resumable append).
    Order MUST match REVIEW_HEADER; the trailing file_artist/file_title are review context."""
    decision = "apply" if p.confidence is Confidence.HIGH else "skip"
    return [
        decision,
        p.fixer,
        p.confidence.value,
        p.track_path,
        p.field,
        _quote_formula(p.current),
        _quote_formula(p.proposed),
        _quote_formula(p.evidence.reason),
        _quote_formula(p.file_artist),
        _quote_formula(p.file_title),
    ]


def write_review(proposals: Iterable[Proposal], csv_path: str) -> int:
    """Write the review CSV atomically (temp + os.replace), so a crash mid-write can't leave a
    truncated CSV -- same durability as every in-place rewrite (adjudicate/shazam-filter/...)."""
    rows = list(proposals)

    def write(fh: TextIO) -> None:
        w = csv.writer(fh)
        w.writerow(REVIEW_HEADER)
        w.writerows(_proposal_row(p) for p in rows)

    atomic_write(csv_path, write)
    return len(rows)


def append_review(proposals: Iterable[Proposal], csv_path: str) -> None:
    """Append proposals to a review CSV, writing the header only when the file is new/empty. The
    resumable-scan checkpoint: each file's rows land on disk as it is processed, so a killed run
    keeps its progress. Append is the resume mechanism, so it can't be an atomic full rewrite."""
    ensure_parent(csv_path)
    fresh = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if fresh:
            w.writerow(REVIEW_HEADER)
        w.writerows(_proposal_row(p) for p in proposals)


def read_review(csv_path: str) -> list[ReviewRow]:
    """Read the review CSV into typed rows. Validates the header carries every REQUIRED column
    BEFORE reading, so a hand-edit that renames/drops a load-bearing column fails loudly here
    instead of silently yielding '' at apply time. The file_* context columns are optional (an old
    CSV predates them); extra columns are allowed (a user annotation)."""
    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_REVIEW_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"review CSV {csv_path} is missing column(s): {', '.join(missing)}")
        return [ReviewRow.from_dict({k: _unquote_formula(v or "") for k, v in r.items() if k}) for r in reader]


def write_review_rows(rows: Iterable[ReviewRow], csv_path: str) -> None:
    """Rewrite the review CSV atomically (see atomicio.atomic_write). adjudicate/disambiguate/
    shazam-filter/scrobble-check rewrite IN PLACE -- some over slow agent/network results -- so a
    crash mid-write must not truncate the staged state to header-only."""

    def write(fh: TextIO) -> None:
        w = csv.DictWriter(fh, fieldnames=REVIEW_HEADER)
        w.writeheader()
        w.writerows({k: _quote_formula(v) for k, v in r.to_dict().items()} for r in rows)

    atomic_write(csv_path, write)
