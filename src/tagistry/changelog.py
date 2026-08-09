"""The append-only change log: every write Tagistry makes, made reversible.

One JSONL line per change, in four kinds -- a tag-field edit, a file rename, an embedded cover, a
folder cover.jpg sidecar. Each is a ChangeEntry that knows how to revert() ITSELF, so undo is one
polymorphic call, not a nested if/elif keyed on magic strings. A new kind is a new subclass with
its own revert(); the revert logic can no longer silently go missing on the one net that must never
lose data. This is the imperative shell for the log: all the file I/O lives here.

Every change is addressable: a content-derived `id` names one change and a `run` id groups the
whole batch one command wrote. So undo is not only "the last n" -- it takes an id, a run, or a
path glob, and previews the selection before touching anything.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, ClassVar, TextIO

from . import tagio
from .atomicio import atomic_write
from .domain import ApplyResult, FileLockedError, StaleChangeError

# Populated by @change_kind, so the marker map cannot drift from the classes it dispatches to
_KINDS: dict[str, type[ChangeEntry]] = {}


def change_kind(cls: type[ChangeEntry]) -> type[ChangeEntry]:
    """Register a ChangeEntry subclass under its own `kind` -- the marker it writes into the JSONL
    'field' column to name its kind. The marker lives on the class (one source), so this decorator
    takes no argument. It is kept OFF the class name on purpose: the marker is a persisted on-disk
    format, so renaming the class must not change it."""
    _KINDS[cls.kind] = cls
    return cls


def digest_of(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data else ""


def entry_id(entry: ChangeEntry) -> str:
    """A short stable name for one logged change, derived from its content. Undo drops the lines it
    reverts, so a line NUMBER would name a different change a minute later; this id does not move.

    12 hex chars, not 8: one library-wide apply logs tens of thousands of changes, and at 32 bits
    two of them collide often enough to matter. `undo --id` would then revert both."""
    raw = f"{entry.ts}|{entry.path}|{entry.field}|{entry.new}|{entry.run}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _short(value: str) -> str:
    """Cap a display value: a cover entry's `old` holds base64 art, which must not reach a terminal."""
    return value if len(value) <= 60 else value[:57] + "..."


@dataclass(frozen=True, slots=True)
class ChangeEntry:
    """A reversible tag-field edit: revert writes the old value back. Base for the other kinds.
    An empty `kind` marks the base (a tag edit); its `field` is a real tag name.

    Every revert() first checks the target still holds what Tagistry wrote. A value someone else
    changed since is left alone (StaleChangeError) -- undo must never destroy a newer edit.
    """

    kind: ClassVar[str] = ""

    ts: float
    path: str
    field: str
    old: str
    new: str
    digest: str = ""  # sha256 of the bytes written, for the kinds whose payload is not `new`
    run: str = ""  # the batch that wrote it, so one command's changes undo as one group

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ts": self.ts, "path": self.path, "field": self.field, "old": self.old, "new": self.new}
        if self.digest:  # omitted when unset, so a log written before this field round-trips unchanged
            d["digest"] = self.digest
        if self.run:
            d["run"] = self.run
        return d

    def row(self) -> dict[str, str]:
        """A display row for `changes` / undo output: the id and run that address it, plus what it did."""
        return {
            "id": entry_id(self),
            "run": self.run,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "kind": self.kind.strip("_") or "tag",
            "path": self.path,
            "field": self.field,
            "old": _short(self.old),
            "new": _short(self.new),
        }

    def revert(self) -> None:
        current = tagio.read(self.path).get(self.field)
        if current == self.old:
            return  # already back at the old value
        if current != self.new:
            raise StaleChangeError(self.path, f"{self.field} is {current!r}, expected {self.new!r}")
        tagio.write(self.path, {self.field: self.old})


@change_kind
@dataclass(frozen=True, slots=True)
class RenameChange(ChangeEntry):
    """A file rename (old holds the old path, new the new). Revert renames it back."""

    kind: ClassVar[str] = "__rename__"

    def row(self) -> dict[str, str]:
        # two absolute paths truncate to the same unreadable prefix; the names are what changed
        return {**super().row(), "old": Path(self.old).name, "new": Path(self.new).name}

    def revert(self) -> None:
        if not Path(self.new).exists() and Path(self.old).exists():
            return  # already renamed back
        tagio.rename_file(self.new, self.old)


@change_kind
@dataclass(frozen=True, slots=True)
class CoverChange(ChangeEntry):
    """An embedded front cover (old holds base64 of the art we overwrote, empty if none).
    Revert restores the original art, or removes the art we added to an art-less file."""

    kind: ClassVar[str] = "__cover__"

    def revert(self) -> None:
        current = tagio.read_front_image(self.path)
        if self.digest and digest_of(current) != self.digest:
            raise StaleChangeError(self.path, "the embedded cover is no longer the one tagistry wrote")
        if self.old:
            tagio.set_front_image(self.path, base64.b64decode(self.old))
        else:
            tagio.clear_front_image(self.path)


@change_kind
@dataclass(frozen=True, slots=True)
class FolderCoverChange(ChangeEntry):
    """A cover.jpg sidecar we wrote (path is the file). Revert deletes it."""

    kind: ClassVar[str] = "__foldercover__"

    def revert(self) -> None:
        p = Path(self.path)
        if not p.exists():
            return  # already deleted
        if self.digest and digest_of(p.read_bytes()) != self.digest:
            raise StaleChangeError(self.path, "the sidecar is no longer the one tagistry wrote")
        p.unlink()


def entry_from_dict(d: Mapping[str, Any]) -> ChangeEntry:
    """Build the right ChangeEntry kind from a JSONL row (a normal field -> a tag edit)."""
    cls = _KINDS.get(str(d.get("field", "")), ChangeEntry)
    return cls(
        ts=float(d.get("ts", 0.0) or 0.0),
        path=str(d.get("path", "")),
        field=str(d.get("field", "")),
        old=str(d.get("old", "")),
        new=str(d.get("new", "")),
        digest=str(d.get("digest", "")),
        run=str(d.get("run", "")),
    )


# --- batched writer ---------------------------------------------------------


class LogSession:
    """One open change-log file for a whole apply/rename/cover batch. The handle opens LAZILY on
    the first real write -- so a dry-run or an all-skip batch never creates the file -- then stays
    open across the loop instead of reopening per change, and flushes after each entry so a crash
    still leaves every already-applied change logged (undo-able).

    The session stamps its own `run` id on every entry it writes, so `undo --run` reverses exactly
    the batch one command made."""

    def __init__(self, path: str, run: str = "") -> None:
        self._path = path
        self._run = run or uuid.uuid4().hex[:8]
        self._fh: TextIO | None = None

    @property
    def run(self) -> str:
        return self._run

    def _write(self, entry: ChangeEntry) -> None:
        entry = replace(entry, run=self._run)  # stamped here, so no writer below can forget it
        if self._fh is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            # newline="" -> LF-only log on every OS, matching the atomic rewrite (no CRLF drift)
            self._fh = open(self._path, "a", encoding="utf-8", newline="")  # noqa: SIM115 -- closed in close()
        self._fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())  # flush() only reaches the OS buffer; a power cut still loses it

    def tag_changes(self, path: str, old: dict[str, str], new: dict[str, str]) -> None:
        ts = time.time()
        for name, value in new.items():
            self._write(ChangeEntry(ts, path, name, old[name], value))

    def rename(self, old: str, new: str) -> None:
        self._write(RenameChange(time.time(), new, RenameChange.kind, old, new))

    def cover(self, path: str, source: str, new_art: bytes, old_art: bytes | None = None) -> None:
        # old is the art we overwrote, so undo restores that exact image instead of clearing all art
        old = base64.b64encode(old_art).decode("ascii") if old_art else ""
        self._write(CoverChange(time.time(), path, CoverChange.kind, old, source, digest_of(new_art)))

    def folder_cover(self, path: str, data: bytes) -> None:
        self._write(FolderCoverChange(time.time(), path, FolderCoverChange.kind, "", "sidecar", digest_of(data)))

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


@contextmanager
def open_log(changes_log: str) -> Iterator[LogSession]:
    """A LogSession for the batch, closed on exit. Opens the file only if something is written."""
    session = LogSession(changes_log)
    try:
        yield session
    finally:
        session.close()


# --- read / undo / status ---------------------------------------------------


def read_log(log: Path) -> list[ChangeEntry]:
    """Parse the change log into typed entries, skipping any torn/half-written line. A crash
    mid-append can leave the last line incomplete; one bad line must not brick undo/status."""
    entries: list[ChangeEntry] = []
    # errors="replace": a byte-torn line must cost that ONE line, not raise and brick every undo
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(entry_from_dict(json.loads(line)))
        except ValueError:
            continue
    return entries


def list_changes(changes_log: str, limit: int = 0) -> list[dict[str, str]]:
    """Every undo-able change, newest first, as display rows. limit<=0 means all of them."""
    log = Path(changes_log)
    if not log.exists():
        return []
    rows = [e.row() for e in reversed(read_log(log))]
    return rows[:limit] if limit > 0 else rows


def select(
    entries: Sequence[ChangeEntry], *, n: int = 1, ids: Sequence[str] = (), run: str = "", path: str = ""
) -> list[int]:
    """Indices of the entries the selectors name, oldest first. `ids` names changes one by one;
    `run` and `path` are FILTERS, so giving both narrows to that run's matching files. The two
    halves add up: `--id X --run Y` is X plus all of Y. No selector at all falls back to the last
    n -- the plain `undo 3` stack behaviour."""
    if not (ids or run or path):
        return list(range(max(0, len(entries) - n), len(entries))) if n > 0 else []
    wanted = {i.lower() for i in ids}
    picked = {i for i, e in enumerate(entries) if entry_id(e) in wanted}
    if run or path:
        picked |= {
            i for i, e in enumerate(entries) if (not run or e.run == run) and (not path or _path_matches(e.path, path))
        }
    return sorted(picked)


def _path_matches(logged: str, pattern: str) -> bool:
    """Match a glob against the whole path or just the file name, so `--path 'Karma*.mp3'` works."""
    return fnmatch(logged, pattern) or fnmatch(Path(logged).name, pattern)


def _blockers(entries: Sequence[ChangeEntry]) -> dict[int, int]:
    """Entry index -> index of the NEXT rename that moves that entry's file, where one exists.
    Reverting a change alone once its file has moved would hit a path that is no longer there.

    One backward pass, so `undo --run` over a library-sized log stays linear: walking forward per
    selected entry instead is quadratic, and a whole-library apply logs tens of thousands of rows.
    """
    blocked: dict[int, int] = {}
    next_rename: dict[str, int] = {}  # holds only renames AFTER the index being looked at
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if (later := next_rename.get(entry.path)) is not None:
            blocked[i] = later
        if isinstance(entry, RenameChange):
            next_rename[entry.old] = i
    return blocked


@dataclass(slots=True)
class UndoResult(ApplyResult):
    """An undo outcome, plus the rows it reverted -- or, with dry_run, would revert."""

    reverted: list[dict[str, str]] = field(default_factory=list)


def undo(
    changes_log: str,
    n: int = 1,
    *,
    ids: Sequence[str] = (),
    run: str = "",
    path: str = "",
    dry_run: bool = False,
) -> UndoResult:
    """Reverse the selected changes (see select: by id, by run, by path glob, else the last n);
    drop only the reverted ones. Newest first, so a batch that renamed and retagged one file
    unwinds in the right order. dry_run reports the selection and touches nothing.

    An entry whose revert fails (locked/error) stays in the log so it can be retried -- the log
    never loses a change that is still on disk. The rewrite is atomic (temp + os.replace), so a
    crash mid-truncate can't corrupt the log.
    """
    result = UndoResult()
    log = Path(changes_log)
    if not log.exists():
        return result
    entries = read_log(log)
    selected = set(select(entries, n=n, ids=ids, run=run, path=path))
    if not selected:
        return result
    blocked = _blockers(entries)
    failed: set[int] = set()
    for idx in sorted(selected, reverse=True):  # newest first
        entry = entries[idx]
        if (blocker := blocked.get(idx)) is not None and blocker not in selected:
            name = entry_id(entries[blocker])
            result.errors.append(f"{entry.path}: renamed later by change {name} -- undo that one first")
            failed.add(idx)
            continue
        try:
            if not dry_run:
                entry.revert()
            result.applied += 1
            result.reverted.append(entry.row())
        except FileLockedError:
            result.locked.append(entry.path)
            failed.add(idx)
        except Exception as exc:
            result.errors.append(f"{entry.path}: {exc}")
            failed.add(idx)
    if dry_run:
        return result
    kept = [e for i, e in enumerate(entries) if i not in selected or i in failed]
    _write_text_atomic(log, "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in kept))
    return result


def status(changes_log: str) -> dict[str, object]:
    log = Path(changes_log)
    if not log.exists():
        return {"applied_changes": 0, "last": None}
    entries = read_log(log)
    last = entries[-1].row() if entries else None  # the display row, so its undo id is right there
    return {"applied_changes": len(entries), "last": last}


def _write_text_atomic(path: Path, text: str) -> None:
    """Rewrite the log atomically (see atomicio.atomic_write): unique temp + os.replace, cleaned
    up on any failure, so a crash mid-truncate can never corrupt the undo net."""
    atomic_write(path, lambda fh: fh.write(text))
