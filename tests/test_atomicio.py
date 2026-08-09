"""atomic_write's contract: a reader never sees a partial file, and a failed write leaves
neither an orphaned temp nor a corrupted/created target. The failure path is the whole point
of the helper, so it is the part that must be pinned."""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

import pytest

from tagistry.atomicio import atomic_write


def test_writes_and_replaces_target(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"
    atomic_write(p, lambda fh: fh.write("hello"))
    assert p.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.glob("*.tmp")) == []  # temp swapped in, none left


def test_creates_missing_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "out.txt"
    atomic_write(p, lambda fh: fh.write("x"))
    assert p.read_text(encoding="utf-8") == "x"


def test_failed_write_leaves_no_temp_and_no_target(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"

    def boom(fh: TextIO) -> None:
        raise RuntimeError("write failed mid-way")

    with pytest.raises(RuntimeError, match="write failed"):
        atomic_write(p, boom)
    assert not p.exists()  # a failed first write never creates the target
    assert list(tmp_path.glob("*.tmp")) == []  # the temp was cleaned up


def test_failed_write_preserves_the_existing_target(tmp_path: Path) -> None:
    # A crash mid-write keeps the OLD file: the write goes to a temp and os.replace only runs on success.
    p = tmp_path / "out.txt"
    p.write_text("original", encoding="utf-8")

    def boom(fh: TextIO) -> None:
        fh.write("partial garbage")
        raise RuntimeError("crash after a partial write")

    with pytest.raises(RuntimeError):
        atomic_write(p, boom)
    assert p.read_text(encoding="utf-8") == "original"  # untouched
    assert list(tmp_path.glob("*.tmp")) == []
