"""Tag read + atomic write, over mediafile/mutagen.

mediafile's save() is not atomic (mutagen issue #241). We write into a temp copy
in the same directory, preserve the original mtime, then os.replace() it in.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from mediafile import Image, ImageType, MediaFile, UnreadableFileError

from .domain import FileLockedError, ProgressFn, Track

logger = logging.getLogger(__name__)

# The audio containers Tagistry reads. A library scan walks a tree and keeps only these.
AUDIO_EXTS = {".mp3", ".opus", ".flac", ".m4a", ".ogg"}

# year/track/disc are numeric in the file; carried as strings here (empty = absent)
FIELDS = ("artist", "title", "album", "albumartist", "genre", "year", "track", "disc")
_NUMERIC_FIELDS = frozenset({"year", "track", "disc"})

_RETRIES = 3
_RETRY_WAIT = 0.25
# Both clear on their own: a player holding the handle, or an AV scanning the fresh temp
_TRANSIENT = (PermissionError, UnreadableFileError)


def read(path: str) -> Track:
    """Read a file into a Track. Raises UnreadableFileError on a bad file."""
    mf = MediaFile(path)
    tags = {f: _read_tag(mf, f) for f in FIELDS}
    return Track(
        path=path,
        ext=Path(path).suffix.lstrip(".").lower(),
        tags=tags,
        length=float(mf.length or 0.0),
        bitrate=int(mf.bitrate or 0),
        codec=str(mf.format or ""),
    )


def write(path: str, changes: dict[str, str]) -> None:
    """Write tag changes atomically, preserving mtime. Retries on lock, then raises.

    changes maps field name -> new value. Empty dict is a no-op.
    """
    if not changes:
        return
    unknown = set(changes) - set(FIELDS)
    if unknown:  # a typo'd/foreign field would setattr a dead attr and write nothing -- fail loud
        raise ValueError(f"unknown tag field(s): {sorted(unknown)}")
    bad_numeric = {
        k
        for k in changes
        if k in _NUMERIC_FIELDS and changes[k].strip() and not changes[k].strip().lstrip("-").isdigit()
    }
    if bad_numeric:  # 'year': 'abc' would crash int() mid-write -- reject clearly, upfront
        raise ValueError(f"non-numeric value for {sorted(bad_numeric)}")

    def mutate(mf: MediaFile) -> None:
        for k, v in changes.items():
            # numeric fields are ints in the file: '' clears them, else parse (mediafile stores int)
            setattr(mf, k, (int(v) if v.strip() else None) if k in _NUMERIC_FIELDS else v)

    _atomic_with_retry(path, mutate)


def _read_tag(mf: MediaFile, field: str) -> str:
    """A tag value as a string, '' when absent. year/track/disc come back as ints -> stringify."""
    value = getattr(mf, field, None)
    return "" if value is None else str(value)


def read_images(path: str) -> list[Image]:
    """Every embedded picture on the file (front cover, back, etc.)."""
    return list(MediaFile(path).images or [])


def read_front_image(path: str) -> bytes | None:
    """Raw bytes of the front cover (or the first image), or None if the file has no art."""
    imgs = read_images(path)
    if not imgs:
        return None
    front = next((i for i in imgs if i.type == ImageType.front), imgs[0])
    return bytes(front.data) if front.data else None


def _other_images(mf: MediaFile) -> list[Image]:
    """Every embedded picture except the front cover -- back art, booklet scans, artist photos."""
    return [i for i in mf.images or [] if i.type != ImageType.front]


def set_front_image(path: str, data: bytes) -> None:
    """Replace only the FRONT cover, keeping every other embedded picture. Atomic + lock-safe.
    mediafile maps it to the right container per format (APIC / FLAC Picture / MP4 covr / Opus)."""
    front = Image(data=data, desc="", type=ImageType.front)
    _atomic_with_retry(path, lambda mf: setattr(mf, "images", [front, *_other_images(mf)]))


def clear_front_image(path: str) -> None:
    """Remove the front cover, keeping every other embedded picture. Atomic + lock-safe."""
    _atomic_with_retry(path, lambda mf: setattr(mf, "images", _other_images(mf)))


def clear_images(path: str) -> None:
    """Remove all embedded art. Atomic + lock-safe."""
    _atomic_with_retry(path, lambda mf: setattr(mf, "images", []))


def rename_file(old_path: str, new_path: str) -> None:
    """Rename a file, lock-safe. No-op if the paths are the same; never overwrites an
    existing different file (raises FileExistsError); retries a player lock, then raises
    FileLockedError. os.rename is atomic within a volume."""
    if os.path.abspath(old_path) == os.path.abspath(new_path):
        return
    # samefile, not normcase: normcase does not case-fold on macOS, so a case-only rename raises
    if os.path.exists(new_path) and not os.path.samefile(old_path, new_path):
        raise FileExistsError(new_path)
    last_exc: OSError | None = None
    for attempt in range(_RETRIES):
        try:
            os.rename(old_path, new_path)
            return
        except FileExistsError:
            raise
        except PermissionError as exc:  # Windows: a player holds the handle
            last_exc = exc
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_WAIT)
    raise FileLockedError(old_path) from last_exc


def _atomic_with_retry(path: str, mutate: Callable[[MediaFile], None]) -> None:
    """Apply mutate to a temp copy and os.replace it in, retrying a transient lock/AV race with
    jittered backoff, then raising FileLockedError (chaining the true cause). Shared by tag writes
    and image edits."""
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            _atomic_mutate(path, mutate)
            return
        except _TRANSIENT as exc:  # player holds the handle, or AV scanned the temp mid-write
            last_exc = exc
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_WAIT * (attempt + 1) + random.uniform(0, _RETRY_WAIT))  # noqa: S311 -- jitter, not crypto
    # Outlasting the retries means a mislabeled file (an MP3 named .flac), not a lock
    if isinstance(last_exc, PermissionError):
        raise FileLockedError(path) from last_exc
    raise UnreadableFileError(path, "content does not match the file extension") from last_exc


def make_writable(target: str | Path) -> None:
    # chmod(S_IWRITE) SETS mode 0o200 on POSIX -- write-only, so the next read fails. OR it in instead.
    os.chmod(target, stat.S_IMODE(os.stat(target).st_mode) | stat.S_IWRITE)


def _atomic_mutate(path: str, mutate: Callable[[MediaFile], None]) -> None:
    st = os.stat(path)
    readonly = not st.st_mode & stat.S_IWRITE  # a read-only source (archive/ripped set) blocks the write
    tmp = f"{path}.tagistry.{uuid.uuid4().hex}.tmp"  # unique: two writers can't clash
    shutil.copy2(path, tmp)  # copies content + mode + mtime
    try:
        if readonly:
            make_writable(tmp)  # the temp copied read-only; make it writable to edit
        mf = MediaFile(tmp)
        mutate(mf)
        mf.save()
        os.utime(tmp, (st.st_atime, st.st_mtime))  # save() bumped mtime; restore
        if readonly:
            make_writable(path)  # clear read-only on the target so os.replace can overwrite it
        os.replace(tmp, path)
        if readonly:
            with contextlib.suppress(OSError):  # tags are already in; a failed restore must not raise
                os.chmod(path, stat.S_IMODE(st.st_mode))  # restore the original read-only attribute
    except BaseException:  # any failure (incl. UnreadableFileError on the temp): clean up, propagate
        # a still-unremovable temp is left for `tagistry clean` -- never mask the real error
        with contextlib.suppress(OSError):
            make_writable(tmp)
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise


# --- library scan (read many) + temp cleanup -------------------------------


def iter_audio(root: str) -> Iterator[Path]:
    """Yield every audio file under root, sorted, filtered to AUDIO_EXTS. Filter by extension
    BEFORE the stat + sort: a large library is mostly non-audio (art, .cue/.log/.m3u, dot-dirs),
    so this skips an is_file() syscall per non-audio path and sorts only the audio subset, not the
    whole tree. Same files, same order (sorting a subset of a total order is stable)."""
    audio = (p for p in Path(root).rglob("*") if p.suffix.lower() in AUDIO_EXTS and p.is_file())
    yield from sorted(audio)


def read_tracks(root: str, progress: ProgressFn | None = None) -> list[Track]:
    """Read every audio file under root into a Track, skipping (and logging) unreadable ones, so a
    corrupt file cannot vanish silently from a large scan (--verbose shows each skip).

    `progress(done, total, path)` is called once per file. Reading tags is the slow step on a big
    library, so the read-only commands (doctor/duplicates/rename/...) pass it to stay non-silent.
    The file list is materialized first (a fast dir walk) so `total` is known up front."""
    paths = list(iter_audio(root))
    total = len(paths)
    tracks: list[Track] = []
    skipped = 0
    for i, p in enumerate(paths, 1):
        if progress is not None:
            with contextlib.suppress(Exception):
                progress(i, total, str(p))  # cosmetic: a reporting error must never abort the read
        try:
            tracks.append(read(str(p)))
        except Exception as exc:
            skipped += 1
            logger.warning("skipping unreadable file %s: %s", p, exc)
    if skipped:
        logger.warning("read_tracks: skipped %d unreadable file(s) under %s", skipped, root)
    return tracks


# One survives only when its cleanup was blocked; it was never swapped in, so deleting is safe
_TEMP_GLOB = "*.tagistry.*.tmp"


def clean_temp_files(root: str, dry_run: bool = False) -> tuple[int, list[str]]:
    """Delete orphaned atomic-write temp files under root. Returns (deleted, still_locked). Clears
    a read-only attribute first (temps copy it from a read-only source, then can't be deleted)."""
    deleted, locked = 0, []
    for p in Path(root).rglob(_TEMP_GLOB):
        if dry_run:
            deleted += 1
            continue
        try:
            p.unlink()
            deleted += 1
        except PermissionError:
            try:
                make_writable(p)  # read-only copied from the source; clear it, then delete
                p.unlink()
                deleted += 1
            except OSError:
                locked.append(str(p))
        except OSError:
            locked.append(str(p))
    return deleted, locked
