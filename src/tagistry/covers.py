"""Cover art: embed a front cover into each art-less file, or write one cover.jpg per album folder.

The folder sidecar is the disk-cheap default (one image per album, which Plex/Kodi read); embedding
is opt-in. Every write is logged (changelog) so undo removes the art / deletes the sidecar. The
fetcher is the CoverFetcher boundary type, injected so the CLI wires the real cascade and tests a fake.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from . import changelog, tagio
from .domain import ApplyResult, FileLockedError, Track
from .providers import CoverFetcher
from .tagio import read_tracks

logger = logging.getLogger(__name__)

_EXISTING_COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png"}


def embed_covers(
    root: str, fetcher: CoverFetcher, changes_log: str, replace: bool = False, dry_run: bool = False
) -> ApplyResult:
    """Fetch and embed a front cover for each file missing one (or all, with replace). Needs
    both artist and title to search. Every embed is logged so undo removes the art we added."""
    result = ApplyResult()
    with changelog.open_log(changes_log) as log:
        for track in read_tracks(root):
            try:
                has_art = bool(tagio.read_images(track.path))
            except Exception as exc:
                logger.debug("cover: cannot read images for %s: %s", track.path, exc)
                continue
            artist, title = track.get("artist").strip(), track.get("title").strip()
            if (has_art and not replace) or not artist or not title:
                result.skipped += 1
                continue
            got = fetcher.fetch(artist, title)
            if got is None:
                result.skipped += 1
                continue
            data, source = got
            if dry_run:
                result.applied += 1
                continue
            # Replacing existing art: keep the original so undo can restore it (not wipe it).
            old_art = tagio.read_front_image(track.path) if has_art else None
            try:
                tagio.set_front_image(track.path, data)
            except FileLockedError:
                result.locked.append(track.path)
                continue
            except Exception as exc:
                result.errors.append(f"{track.path}: cover embed failed ({exc})")
                continue
            log.cover(track.path, source, data, old_art)
            result.applied += 1
    return result


def _dominant(values: Iterable[str]) -> str:
    counts = Counter(v.strip() for v in values if v.strip())
    return counts.most_common(1)[0][0] if counts else ""


def _cover_ext(data: bytes) -> str:
    return ".png" if data[:8].startswith(b"\x89PNG") else ".jpg"


def save_folder_covers(root: str, fetcher: CoverFetcher, changes_log: str, dry_run: bool = False) -> ApplyResult:
    """Write ONE cover.jpg per album folder that has none — the disk-cheap default (one image per
    folder, not embedded into every track). Searches by the folder's dominant artist + album.
    Every file written is logged so undo deletes it."""
    result = ApplyResult()
    by_folder: dict[str, list[Track]] = {}
    for track in read_tracks(root):
        by_folder.setdefault(str(Path(track.path).parent), []).append(track)
    with changelog.open_log(changes_log) as log:
        for folder, tracks in by_folder.items():
            fp = Path(folder)
            existing = {p.name.lower() for p in fp.iterdir() if p.is_file()}
            if existing & _EXISTING_COVER_NAMES:
                result.skipped += 1
                continue
            artist = _dominant(t.get("artist") for t in tracks)
            query = _dominant(t.get("album") for t in tracks) or _dominant(t.get("title") for t in tracks)
            if not artist or not query:
                result.skipped += 1
                continue
            got = fetcher.fetch(artist, query)
            if got is None:
                result.skipped += 1
                continue
            data, _source = got
            if dry_run:
                result.applied += 1
                continue
            cover_path = str(fp / ("cover" + _cover_ext(data)))
            try:
                Path(cover_path).write_bytes(data)
            except Exception as exc:
                result.errors.append(f"{folder}: cover write failed ({exc})")
                continue
            log.folder_cover(cover_path, data)
            result.applied += 1
    return result
