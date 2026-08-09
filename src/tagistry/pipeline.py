"""The pipeline: scan -> propose -> review store -> apply reversibly.

This module owns the two ends of the flow -- scan (read the library, run fixers, de-conflict into
proposals) and apply (write the kept rows to disk, reversibly). The middle is split out and the
neighbours own the rest; everything is re-exported here as one facade (`pipeline.X`) for the
CLI/MCP/tests:
- the staged-CSV format lives in reviewstore.py;
- the gate decisions (adjudicate/disambiguate/shazam-filter/scrobble-check/markers) in gates.py;
- provider construction in assembly.py, the rename flow in rename.py, the reversible change log in
  changelog.py, cover art in covers.py, read-only audits in audits.py.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from . import changelog, config, tagio
from .assembly import make_researcher  # re-exported: construction is assembly's
from .changelog import list_changes, status, undo  # re-exported: the log is changelog's, this is the facade
from .domain import ApplyResult, Confidence, FileLockedError, ProgressFn, Proposal
from .fixers import FIXERS, priority
from .gates import (  # re-exported: the gate decisions are gates.py's, pipeline stays the facade
    adjudicate,
    check_scrobble_coverage,
    direction_digest,
    disambiguate,
    markers,
    shazam_filter,
)
from .providers import LibraryPrior, Providers
from .rename import (  # re-exported: renaming is rename.py's decision, pipeline stays the facade
    apply_renames,
    classify_album_folders,
    read_rename_plan,
    rename_plan,
    write_rename_plan,
)
from .reviewstore import (  # re-exported: the staged-CSV format is reviewstore's
    REVIEW_HEADER,
    append_review,
    read_review,
    write_review,
)
from .tagio import clean_temp_files, read_tracks  # scan primitives; re-exported for the CLI/MCP
from .text import canonicalize

# One import surface (`pipeline.X`); __all__ also marks the imports above intentional (F401)
__all__ = [
    "REVIEW_HEADER",
    "adjudicate",
    "append_review",
    "apply_renames",
    "check_scrobble_coverage",
    "classify_album_folders",
    "clean_temp_files",
    "direction_digest",
    "disambiguate",
    "list_changes",
    "make_researcher",
    "markers",
    "read_rename_plan",
    "read_review",
    "read_tracks",
    "rename_plan",
    "shazam_filter",
    "status",
    "undo",
    "write_rename_plan",
    "write_review",
]

logger = logging.getLogger(__name__)

# Never CWD: the change log is the only undo net, so `apply` with no --log must not scatter it
DEFAULT_REVIEW = config.review_path()
DEFAULT_LOG = config.log_path()
DEFAULT_MARKERS = str(config.base_dir() / "markers.csv")
DEFAULT_VERDICTS = str(config.base_dir() / "shazam_verdicts.csv")

# Pinned to the registry at import: a @fixer rename leaving this stale would fail OPEN
ATOMIC_FIXERS = {"flip", "feat_to_title"}
if _unknown_atomic := ATOMIC_FIXERS - set(FIXERS):
    raise RuntimeError(f"ATOMIC_FIXERS names an unknown fixer: {_unknown_atomic}")

_CONF_RANK = {Confidence.HIGH: 3, Confidence.REVIEW: 2, Confidence.LOW: 1}


def scan(
    root: str,
    providers: Providers | None = None,
    fixers: Iterable[str] | None = None,
    progress: ProgressFn | None = None,
    checkpoint: str | None = None,
) -> list[Proposal]:
    """Read the library, run fixers, return de-conflicted proposals.

    LibraryPrior is built from the full library first (offline self-consistency), unless the caller
    already supplied one. `progress(done, total, path)` is called once per file so an online scan
    (rate-limited to 1 req/s) isn't a silent wait. With `checkpoint` set (a review CSV path), each
    file's proposals are APPENDED to it as the file is processed, and any path already in that CSV
    is skipped -- so a killed --fingerprint scan (fpcalc is not cached) resumes instead of redoing
    every file. Returns only the proposals produced THIS run (the resumed ones are already on disk).
    """
    providers = providers or Providers()
    tracks = read_tracks(root)
    if providers.library is None:
        providers.library = LibraryPrior.from_tracks(tracks)
    names = list(fixers) if fixers is not None else list(FIXERS)
    unknown = [n for n in names if n not in FIXERS]
    if unknown:  # a typo'd --fixers name would silently scan nothing; fail loudly instead
        raise ValueError(f"unknown fixer(s): {', '.join(unknown)}; known: {', '.join(FIXERS)}")
    # Clean files leave no CSV row, so a sidecar marks them -- else --resume re-fingerprints them all
    marker = checkpoint + ".processed" if checkpoint else None
    done: set[str] = set()
    if checkpoint and os.path.exists(checkpoint):
        done = {r.path for r in read_review(checkpoint)}  # files with staged rows
    if marker and os.path.exists(marker):
        done |= set(Path(marker).read_text(encoding="utf-8").splitlines())  # clean files
    if done:
        logger.info("resuming scan: %d file(s) already processed for %s", len(done), checkpoint)
    total = len(tracks)
    proposals: list[Proposal] = []
    for i, track in enumerate(tracks, 1):
        if progress is not None:
            with contextlib.suppress(Exception):
                progress(i, total, track.path)  # cosmetic: a reporting error must never abort the scan
        if track.path in done:
            continue
        file_props: list[Proposal] = []
        for name in names:
            try:
                file_props.extend(FIXERS[name](track, providers))
            except Exception as exc:  # one fixer's error on one file must not abort the scan
                logger.debug("fixer %s failed on %s: %s", name, track.path, exc)
        file_props = dedup(file_props)  # per (path, field); keys are per-file so this is the global dedup
        proposals.extend(file_props)
        if checkpoint:
            append_review(file_props, checkpoint)  # persist this file's rows now, before the next
            if not file_props and marker:  # CSV rows first, then mark clean files -- so a crash never dups
                with open(marker, "a", encoding="utf-8") as fh:
                    fh.write(track.path + "\n")
    return dedup(proposals)


def dedup(proposals: Iterable[Proposal]) -> list[Proposal]:
    """One proposal per (path, field): highest confidence, then fixer priority.

    The winning value is canonicalized (ASCII dashes, fullwidth folded, spaces collapsed)
    so a value fixed by any fixer still comes out canonical. A row whose value collapses
    to the current is dropped.
    """
    best: dict[tuple[str, str], Proposal] = {}
    for p in proposals:
        k = (p.track_path, p.field)
        if k not in best or _rank(p) > _rank(best[k]):
            best[k] = p
    out: list[Proposal] = []
    for p in best.values():
        clean = canonicalize(p.proposed)
        if clean != p.proposed:
            p = replace(p, proposed=clean)
        if p.proposed != p.current:
            out.append(p)
    return sorted(out, key=lambda p: (p.fixer, -_CONF_RANK[p.confidence], p.track_path, p.field))


def _rank(p: Proposal) -> tuple[int, int]:
    return (_CONF_RANK[p.confidence], -priority(p.fixer))


# --- apply + change log -----------------------------------------------------


def apply(csv_path: str, changes_log: str, dry_run: bool = False) -> ApplyResult:
    """Write kept rows atomically; append each change to the undo log.

    Kept = decision column == "apply" and proposed differs from current.
    Idempotent: a field already at its proposed value is skipped.
    A flip is two rows (artist + title); both must be kept together or neither
    applies, so a half-swap can never corrupt a file.
    """
    result = ApplyResult()
    rows = read_review(csv_path)

    # A MIXED decision across an atomic fixer's rows would half-write the file, so drop the group
    atomic_states: dict[tuple[str, str], list[bool]] = {}
    for row in rows:
        if row.fixer in ATOMIC_FIXERS:
            atomic_states.setdefault((row.path, row.fixer), []).append(row.is_apply)
    broken = {grp for grp, states in atomic_states.items() if any(states) and not all(states)}
    for path, fixer in sorted(broken):
        result.errors.append(f"{path}: {fixer} needs its rows kept together — skipped")

    by_path: dict[str, dict[str, tuple[str, str]]] = {}
    for row in rows:
        if not row.is_apply:
            result.skipped += 1
            continue
        if (row.path, row.fixer) in broken:
            result.skipped += 1
            continue
        if row.proposed == row.current:
            result.skipped += 1
            continue
        by_path.setdefault(row.path, {})[row.field] = (row.current, row.proposed)

    with changelog.open_log(changes_log) as log:  # opened once, lazily (never on a dry-run/all-skip)
        for path, fields in by_path.items():
            try:
                track = tagio.read(path)
            except Exception as exc:
                result.errors.append(f"{path}: unreadable ({exc})")
                continue
            # A field no longer holding what the scan staged was edited since -- writing would destroy it
            changes: dict[str, str] = {}
            for f, (was, v) in fields.items():
                now = track.get(f)
                if now == v:
                    result.skipped += 1  # already at the proposed value
                elif now != was:
                    result.skipped += 1
                    result.errors.append(f"{path}: {f} changed since the scan ({now!r}) — re-scan; not applied")
                else:
                    changes[f] = v
            if not changes:
                continue
            if dry_run:
                result.applied += len(changes)
                continue
            try:
                tagio.write(path, changes)
            except FileLockedError:
                result.locked.append(path)
                continue
            except Exception as exc:
                result.errors.append(f"{path}: write failed ({exc})")
                continue
            try:
                log.tag_changes(path, {f: track.get(f) for f in changes}, changes)
            except Exception as exc:
                # The tags are already on disk, so this change is real but outside the undo net
                result.errors.append(f"{path}: applied but NOT logged -- undo unavailable ({exc})")
            result.applied += len(changes)
    return result
