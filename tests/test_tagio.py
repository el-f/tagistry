"""Tag read + atomic write, on real audio samples across formats."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from mediafile import Image, ImageType, MediaFile, UnreadableFileError

from tagistry import tagio
from tagistry.domain import FileLockedError


def test_read_returns_track(audio_file: Path) -> None:
    mf = MediaFile(str(audio_file))
    mf.artist, mf.title = "The Artist", "The Title"
    mf.save()
    track = tagio.read(str(audio_file))
    assert track.get("artist") == "The Artist"
    assert track.get("title") == "The Title"
    assert track.length > 0
    assert track.ext == audio_file.suffix.lstrip(".")


def test_write_changes_tag(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"artist": "New Name", "title": "New Title"})
    track = tagio.read(str(audio_file))
    assert track.get("artist") == "New Name"
    assert track.get("title") == "New Title"


def test_write_preserves_mtime(audio_file: Path) -> None:
    fixed = 1_600_000_000
    os.utime(audio_file, (fixed, fixed))
    tagio.write(str(audio_file), {"artist": "Whoever"})
    assert int(audio_file.stat().st_mtime) == fixed


def test_write_leaves_no_temp(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"title": "X"})
    assert not list(audio_file.parent.glob("*.tagistry.*.tmp"))


def test_write_empty_is_noop(audio_file: Path) -> None:
    before = audio_file.read_bytes()
    tagio.write(str(audio_file), {})
    assert audio_file.read_bytes() == before


def test_read_write_roundtrips_year_genre_track_disc(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"year": "1997", "genre": "Symphonic Metal", "track": "9", "disc": "1"})
    t = tagio.read(str(audio_file))
    assert t.get("year") == "1997" and t.get("genre") == "Symphonic Metal"
    assert t.get("track") == "9" and t.get("disc") == "1"


def test_write_clears_numeric_field_with_empty(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"year": "2020"})
    tagio.write(str(audio_file), {"year": ""})  # empty clears it, doesn't crash on int('')
    assert tagio.read(str(audio_file)).get("year") == ""


def test_write_rejects_unknown_field(audio_file: Path) -> None:
    # a typo'd/foreign field would silently setattr an unused attr and write nothing -- fail loud
    with pytest.raises(ValueError):
        tagio.write(str(audio_file), {"bogus": "x"})


def test_write_rejects_non_numeric_numeric_field(audio_file: Path) -> None:
    with pytest.raises(ValueError):
        tagio.write(str(audio_file), {"year": "abc"})


def test_write_to_readonly_file_succeeds_and_restores_readonly(audio_file: Path) -> None:
    import stat as _stat

    os.chmod(audio_file, _stat.S_IREAD)  # archive/ripped files are often read-only
    try:
        tagio.write(str(audio_file), {"artist": "Written Anyway"})
        assert tagio.read(str(audio_file)).get("artist") == "Written Anyway"
        assert not audio_file.stat().st_mode & _stat.S_IWRITE  # still read-only afterward
    finally:
        os.chmod(audio_file, _stat.S_IWRITE)  # let the tmp dir clean up


def test_readonly_restore_failure_does_not_fail_a_succeeded_write(
    audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After os.replace the write SUCCEEDED, so a failing read-only restore must not raise it as a failure.
    import stat as _stat

    real_chmod = os.chmod
    target = str(audio_file)

    def chmod(path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        # the post-replace restore is the only chmod that sets the ORIGINAL file back to read-only
        if path == target and not mode & _stat.S_IWRITE:
            raise OSError("simulated: cannot restore the read-only bit")
        real_chmod(path, mode, follow_symlinks=follow_symlinks)

    real_chmod(audio_file, _stat.S_IREAD)  # read-only source (archive/ripped set)
    monkeypatch.setattr("tagistry.tagio.os.chmod", chmod)
    try:
        tagio.write(target, {"artist": "Written Anyway"})  # must NOT raise
        assert tagio.read(target).get("artist") == "Written Anyway"
    finally:
        monkeypatch.undo()
        os.chmod(audio_file, _stat.S_IWRITE)  # let the tmp dir clean up


def test_case_only_rename(audio_file: Path) -> None:
    # renaming to a case-variant on a case-insensitive FS must not be refused as "exists"
    new = str(audio_file.parent / audio_file.name.upper())
    if new == str(audio_file):
        return  # already uppercase; nothing to test
    tagio.rename_file(str(audio_file), new)
    assert audio_file.name.upper() in {p.name for p in audio_file.parent.iterdir()}


def test_locked_file_raises_after_retries(audio_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def boom(path: str, mutate: Callable[[MediaFile], None]) -> None:
        calls["n"] += 1
        raise PermissionError("locked by player")

    monkeypatch.setattr(tagio, "_atomic_mutate", boom)
    monkeypatch.setattr("tagistry.tagio.time.sleep", lambda _s: None)
    with pytest.raises(FileLockedError):
        tagio.write(str(audio_file), {"artist": "X"})
    assert calls["n"] == 3  # retried the configured number of times


def test_locked_then_succeeds(audio_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = tagio._atomic_mutate

    def flaky(path: str, mutate: Callable[[MediaFile], None]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked once")
        real(path, mutate)

    monkeypatch.setattr(tagio, "_atomic_mutate", flaky)
    monkeypatch.setattr("tagistry.tagio.time.sleep", lambda _s: None)
    tagio.write(str(audio_file), {"artist": "Recovered"})
    assert tagio.read(str(audio_file)).get("artist") == "Recovered"


def test_unreadable_temp_is_retried_then_succeeds(audio_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An AV scan of the fresh temp throws UnreadableFileError: a transient race, so retry, do not hard-fail.
    calls = {"n": 0}
    real = tagio._atomic_mutate

    def flaky(path: str, mutate: Callable[[MediaFile], None]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnreadableFileError(path, "temp scanned mid-write")
        real(path, mutate)

    monkeypatch.setattr(tagio, "_atomic_mutate", flaky)
    monkeypatch.setattr("tagistry.tagio.time.sleep", lambda _s: None)
    tagio.write(str(audio_file), {"artist": "Recovered"})
    assert calls["n"] == 2
    assert tagio.read(str(audio_file)).get("artist") == "Recovered"


def test_unreadable_temp_exhausts_to_unreadable_not_locked(audio_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A corrupt temp outlasts the AV-scan retry window: report unreadable, not "locked".
    def boom(path: str, mutate: Callable[[MediaFile], None]) -> None:
        raise UnreadableFileError(path, "temp keeps failing")

    monkeypatch.setattr(tagio, "_atomic_mutate", boom)
    monkeypatch.setattr("tagistry.tagio.time.sleep", lambda _s: None)
    with pytest.raises(UnreadableFileError):
        tagio.write(str(audio_file), {"artist": "X"})
    with pytest.raises(FileLockedError):  # a held handle (PermissionError) IS still a lock

        def held(path: str, mutate: Callable[[MediaFile], None]) -> None:
            raise PermissionError("player holds the handle")

        monkeypatch.setattr(tagio, "_atomic_mutate", held)
        tagio.write(str(audio_file), {"artist": "X"})


def test_read_tracks_reports_progress(tmp_path: Path) -> None:
    # Read-only commands drive their bar off this callback: one call per file, total known up front.
    import shutil

    src = Path(__file__).parent / "fixtures" / "sample.mp3"
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        shutil.copy2(src, tmp_path / name)
    calls: list[tuple[int, int, str]] = []
    tracks = tagio.read_tracks(str(tmp_path), progress=lambda d, t, p: calls.append((d, t, p)))
    assert len(tracks) == 3
    assert [d for d, _t, _p in calls] == [1, 2, 3]  # one monotonic call per file
    assert all(t == 3 for _d, t, _p in calls)  # total known before the first read


def _png(marker: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + marker


def test_set_front_image_keeps_other_embedded_art(audio_file: Path) -> None:
    # Replacing the cover must keep back art; the log records only the front image, so undo cannot restore more.
    back = Image(data=_png(b"back"), desc="back", type=ImageType.back)
    front = Image(data=_png(b"old-front"), desc="", type=ImageType.front)
    mf = MediaFile(str(audio_file))
    mf.images = [front, back]
    mf.save()

    tagio.set_front_image(str(audio_file), _png(b"new-front"))

    kinds = {i.type: bytes(i.data) for i in tagio.read_images(str(audio_file))}
    assert kinds[ImageType.front] == _png(b"new-front")
    assert kinds[ImageType.back] == _png(b"back")


def test_clear_front_image_keeps_other_embedded_art(audio_file: Path) -> None:
    mf = MediaFile(str(audio_file))
    mf.images = [
        Image(data=_png(b"front"), desc="", type=ImageType.front),
        Image(data=_png(b"back"), desc="back", type=ImageType.back),
    ]
    mf.save()

    tagio.clear_front_image(str(audio_file))

    remaining = tagio.read_images(str(audio_file))
    assert [i.type for i in remaining] == [ImageType.back]
