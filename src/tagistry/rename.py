"""Rename files to 'artist - title', filesystem-safe, staged and reversible.

Owns one decision: what a clean filename is and which files get one. An album folder (most
files share one album tag + track-number names) is left alone so its ordering survives; loose
and playlist folders are renamed. A rename moves the path the undo log keys on, so it runs
through the same scan -> stage -> apply discipline as tag edits: `rename_plan` proposes,
`write_rename_plan` stages a CSV, `apply_renames` performs and logs each move.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from . import changelog, tagio
from .atomicio import ensure_parent
from .domain import ApplyResult, FileLockedError, Track
from .text import key, safe_filename

_TRACKNUM_FN = re.compile(r"^\s*\d{1,3}\s*[.\-_ ]")  # "01 ", "3. ", "12 - " in a filename


def classify_album_folders(tracks: Iterable[Track]) -> set[str]:
    """Folders that look like one album: a strong majority of the files share one album tag
    AND carry track-number filenames. Renaming inside these would break the album's ordering,
    so `rename` skips them by default. Loose/playlist folders (mixed albums) are renamed."""
    by_folder: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        by_folder[str(Path(t.path).parent)].append(t)
    album: set[str] = set()
    for folder, ts in by_folder.items():
        if len(ts) < 2:
            continue
        albums = Counter(key(t.get("album")) for t in ts if t.get("album").strip())
        if not albums:
            continue
        share = albums.most_common(1)[0][1] / len(ts)
        tracknum = sum(bool(_TRACKNUM_FN.match(Path(t.path).name)) for t in ts) / len(ts)
        if share >= 0.6 and tracknum >= 0.4:
            album.add(folder)
    return album


def rename_plan(tracks: Iterable[Track], rename_all: bool = False) -> list[tuple[str, str]]:
    """Plan renames to 'artist - title.ext' (filesystem-safe). Needs both tags. Skips album
    folders unless rename_all, files already correctly named, and any target collision."""
    tracks = list(tracks)
    album = set() if rename_all else classify_album_folders(tracks)
    plan: list[tuple[str, str]] = []
    targets: set[str] = set()
    for t in tracks:
        folder = Path(t.path).parent
        if str(folder) in album:
            continue
        artist, title = t.get("artist").strip(), t.get("title").strip()
        if not artist or not title:
            continue
        stem = safe_filename(f"{artist} - {title}")
        if not stem:
            continue
        new_path = str(folder / (stem + Path(t.path).suffix.lower()))
        if os.path.abspath(new_path) == os.path.abspath(t.path):
            continue
        if new_path in targets or os.path.exists(new_path):
            continue  # never overwrite or collide two renames onto one name
        targets.add(new_path)
        plan.append((t.path, new_path))
    return plan


RENAME_PLAN_HEADER = ["old_path", "new_path", "reason"]


def write_rename_plan(plan: Iterable[tuple[str, str]], csv_path: str) -> int:
    """Stage a rename plan to a CSV (old_path, new_path, reason) WITHOUT touching the filesystem --
    the same scan -> review -> apply discipline as tag edits. A rename moves the path the undo log
    keys on, so it is the highest-value staging gap: review the CSV, then `apply-renames --plan`."""
    rows = list(plan)
    ensure_parent(csv_path)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(RENAME_PLAN_HEADER)
        for old, new in rows:
            w.writerow([old, new, f"rename to '{Path(new).name}'"])
    return len(rows)


def read_rename_plan(csv_path: str) -> list[tuple[str, str]]:
    """Read a staged rename plan back into (old_path, new_path) pairs. Validates the two required
    columns up front so a hand-edit that drops one fails loudly, not silently as ''."""
    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in ("old_path", "new_path") if c not in header]
        if missing:
            raise ValueError(f"rename plan {csv_path} is missing column(s): {', '.join(missing)}")
        return [(r["old_path"], r["new_path"]) for r in reader if (r.get("old_path") and r.get("new_path"))]


def apply_renames(plan: Iterable[tuple[str, str]], changes_log: str, dry_run: bool = False) -> ApplyResult:
    """Rename each planned file, logging every rename so undo restores the old name. A locked
    or colliding file is skipped, never forced — the batch continues."""
    result = ApplyResult()
    with changelog.open_log(changes_log) as log:
        for old, new in plan:
            if dry_run:
                result.applied += 1
                continue
            try:
                tagio.rename_file(old, new)
            except FileLockedError:
                result.locked.append(old)
                continue
            except FileExistsError:
                result.skipped += 1
                continue
            except Exception as exc:
                result.errors.append(f"{old}: rename failed ({exc})")
                continue
            try:
                log.rename(old, new)
            except Exception as exc:
                # The file is already renamed, so this is real but outside the undo net
                result.errors.append(f"{old}: renamed but NOT logged -- undo unavailable ({exc})")
            result.applied += 1
    return result
