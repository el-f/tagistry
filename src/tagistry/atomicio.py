"""Atomic text-file write: write a unique temp, then os.replace it onto the target.

One helper so the durability guarantee -- a unique temp, an atomic swap, and cleanup on ANY
failure (including KeyboardInterrupt) -- lives in one place instead of being re-derived by the
review CSV, the change log, and the research cache. A reader never sees a half-written file, and
a crash mid-write never corrupts the target or orphans a temp.

NOT tagio._atomic_mutate: that copies the original + preserves mode/mtime + retries a file lock
for an in-place audio-tag edit -- a heavier, different decision that must stay separate.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TextIO


def ensure_parent(path: str | Path) -> None:
    """Create the directory that will hold `path`. The config base dir may not exist yet, so a
    write to a missing parent must create it, not crash. For the non-atomic writers -- atomic_write
    below does its own mkdir."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def atomic_write(path: str | Path, write: Callable[[TextIO], object]) -> None:
    """Write `path` atomically. `write` is handed an open text handle for a unique temp; the temp
    is os.replace'd onto `path` only after `write` returns, and is removed on any failure.

    Opened with newline="" so a csv.writer controls its own line endings; text/JSON callers embed
    their own "\\n", so they get consistent LF output on every platform (no CRLF translation)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")  # unique: two writers can't clash
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            write(fh)
        os.replace(tmp, p)
    except BaseException:  # incl. KeyboardInterrupt: never leave a partial target or orphan temp
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
