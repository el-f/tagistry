"""Typed ChangeEntry: from_dict dispatches to the right kind; each kind reverts itself.

Plus the selection half of undo: every change is addressable by id, by run, or by path glob.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tagistry import changelog, tagio
from tagistry.changelog import ChangeEntry, CoverChange, FolderCoverChange, RenameChange, digest_of, entry_id
from tagistry.domain import StaleChangeError


def test_entry_from_dict_dispatches_by_field_kind() -> None:
    assert type(changelog.entry_from_dict({"field": "title"})) is ChangeEntry  # a normal field -> tag edit
    assert type(changelog.entry_from_dict({"field": "__rename__"})) is RenameChange
    assert type(changelog.entry_from_dict({"field": "__cover__"})) is CoverChange
    assert type(changelog.entry_from_dict({"field": "__foldercover__"})) is FolderCoverChange


def test_entry_round_trips_through_dict() -> None:
    d = {"ts": 1.5, "path": "a.mp3", "field": "title", "old": "A", "new": "B"}
    assert changelog.entry_from_dict(d).to_dict() == d


def test_folder_cover_revert_deletes_the_sidecar(tmp_path: Path) -> None:
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"x")
    FolderCoverChange(1.0, str(cover), FolderCoverChange.kind, "", "sidecar").revert()
    assert not cover.exists()


def test_folder_cover_revert_refuses_to_delete_a_replaced_sidecar(tmp_path: Path) -> None:
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"ours")
    entry = FolderCoverChange(1.0, str(cover), FolderCoverChange.kind, "", "sidecar", digest_of(b"ours"))
    cover.write_bytes(b"the user's own better cover")  # replaced by hand after we wrote it
    with pytest.raises(StaleChangeError):
        entry.revert()
    assert cover.read_bytes() == b"the user's own better cover"  # undo left it alone


def test_tag_revert_refuses_to_clobber_a_newer_edit(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"title": "Tagistry Value"})
    entry = ChangeEntry(1.0, str(audio_file), "title", "Old", "Tagistry Value")
    tagio.write(str(audio_file), {"title": "Newer Manual Value"})  # user edits it after we wrote
    with pytest.raises(StaleChangeError):
        entry.revert()
    assert tagio.read(str(audio_file)).get("title") == "Newer Manual Value"


def test_tag_revert_is_a_noop_when_already_back_at_old(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"title": "Old"})
    ChangeEntry(1.0, str(audio_file), "title", "Old", "Tagistry Value").revert()  # must not raise
    assert tagio.read(str(audio_file)).get("title") == "Old"


def test_tag_revert_restores_the_old_value(audio_file: Path) -> None:
    tagio.write(str(audio_file), {"title": "Tagistry Value"})
    ChangeEntry(1.0, str(audio_file), "title", "Old", "Tagistry Value").revert()
    assert tagio.read(str(audio_file)).get("title") == "Old"


def test_read_log_survives_a_byte_torn_line(tmp_path: Path) -> None:
    # Strict decoding raises before any line is parsed, bricking undo for the whole library
    log = tmp_path / "c.jsonl"
    good = b'{"ts": 1, "path": "a.mp3", "field": "title", "old": "A", "new": "B"}\n'
    log.write_bytes(good + b'{"ts": 2, "path": "\xff\xfe\x80 torn\n' + good)
    entries = changelog.read_log(log)
    assert len(entries) == 2 and all(e.path == "a.mp3" for e in entries)


def test_digest_is_omitted_from_a_dict_when_unset() -> None:
    d = {"ts": 1.5, "path": "a.mp3", "field": "title", "old": "A", "new": "B"}
    assert changelog.entry_from_dict(d).to_dict() == d  # a log written before `digest` round-trips


def test_folder_cover_revert_is_safe_when_already_gone(tmp_path: Path) -> None:
    # undo must not raise if the sidecar was already deleted by hand
    FolderCoverChange(1.0, str(tmp_path / "gone.jpg"), FolderCoverChange.kind, "", "sidecar").revert()


def test_log_session_opens_lazily_and_flushes_per_write(tmp_path: Path) -> None:
    import json

    log = tmp_path / "c.jsonl"
    with changelog.open_log(str(log)) as session:
        assert not log.exists()  # lazy: the file is not created until the first write
        session.tag_changes("a.mp3", {"title": "Old"}, {"title": "New"})
        assert log.exists()  # opened on first write
        # flushed per write: the entry is readable BEFORE close (a crash keeps it)
        assert json.loads(log.read_text(encoding="utf-8").strip())["new"] == "New"
    assert len(changelog.read_log(log)) == 1  # one entry after close


def test_log_session_no_write_never_creates_the_file(tmp_path: Path) -> None:
    log = tmp_path / "c.jsonl"
    with changelog.open_log(str(log)):
        pass  # a dry-run / all-skip batch writes nothing
    assert not log.exists()  # so no empty log is left behind


# --- selection: id, run, path glob ------------------------------------------


def _sidecar(tmp_path: Path, name: str, data: bytes = b"art") -> Path:
    """A written cover.jpg the log can revert by deleting -- a change that needs no audio file."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_entry_id_survives_a_log_rewrite(tmp_path: Path) -> None:
    # undo drops the lines it reverts, so an id must not be a line number
    log = str(tmp_path / "c.jsonl")
    first, second = _sidecar(tmp_path, "a/cover.jpg"), _sidecar(tmp_path, "b/cover.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(first), b"art")
        session.folder_cover(str(second), b"art")
    before = changelog.list_changes(log)[0]["id"]  # newest first: the second sidecar
    changelog.undo(log, ids=[changelog.list_changes(log)[1]["id"]])  # revert the OLDER one
    assert changelog.list_changes(log)[0]["id"] == before
    assert len(before) == 12  # narrowing this makes two changes collide on one id


def test_run_and_path_narrow_each_other(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    art, back = _sidecar(tmp_path, "a/cover.jpg"), _sidecar(tmp_path, "a/back.jpg")
    other_run = _sidecar(tmp_path, "b/cover.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(art), b"art")
        session.folder_cover(str(back), b"art")
        run = session.run
    with changelog.open_log(log) as session:
        session.folder_cover(str(other_run), b"art")

    # both filters, so: that run AND that name -- not the union of the two
    assert changelog.undo(log, run=run, path="cover.jpg").applied == 1
    assert not art.exists() and back.exists() and other_run.exists()


def test_blocker_is_the_nearest_later_rename_not_the_last(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    first, second, third = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    first.write_bytes(b"art")
    with changelog.open_log(log) as session:
        session.folder_cover(str(first), b"art")
        first.rename(second)
        session.rename(str(first), str(second))
        second.rename(third)
        session.rename(str(second), str(third))
    rows = {r["old"]: r["id"] for r in changelog.list_changes(log)}

    result = changelog.undo(log, ids=[next(r["id"] for r in changelog.list_changes(log) if r["kind"] != "rename")])
    assert result.applied == 0 and rows["a.jpg"] in result.errors[0]  # the a->b rename, not b->c


def test_undo_by_id_reverses_only_that_change(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    first, second = _sidecar(tmp_path, "a/cover.jpg"), _sidecar(tmp_path, "b/cover.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(first), b"art")
        session.folder_cover(str(second), b"art")
    target = next(r for r in changelog.list_changes(log) if r["path"] == str(first))

    result = changelog.undo(log, ids=[target["id"]])
    assert result.applied == 1 and [r["id"] for r in result.reverted] == [target["id"]]
    assert not first.exists() and second.exists()  # only the named change came back
    assert [r["path"] for r in changelog.list_changes(log)] == [str(second)]  # and only it left the log


def test_undo_by_unknown_id_reverses_nothing(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    cover = _sidecar(tmp_path, "a/cover.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(cover), b"art")
    assert changelog.undo(log, ids=["deadbeef"]).applied == 0
    assert cover.exists() and len(changelog.list_changes(log)) == 1


def test_undo_by_run_reverses_exactly_one_batch(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    old_one, old_two = _sidecar(tmp_path, "a/cover.jpg"), _sidecar(tmp_path, "b/cover.jpg")
    kept = _sidecar(tmp_path, "c/cover.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(old_one), b"art")
        session.folder_cover(str(old_two), b"art")
        first_run = session.run
    with changelog.open_log(log) as session:
        session.folder_cover(str(kept), b"art")
        assert session.run != first_run  # a second command is a second run

    assert changelog.undo(log, run=first_run).applied == 2
    assert not old_one.exists() and not old_two.exists()
    assert kept.exists() and len(changelog.list_changes(log)) == 1


def test_undo_by_path_glob_matches_the_file_name(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    art, other = _sidecar(tmp_path, "a/cover.jpg"), _sidecar(tmp_path, "a/back.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(art), b"art")
        session.folder_cover(str(other), b"art")
    assert changelog.undo(log, path="cover.jpg").applied == 1
    assert not art.exists() and other.exists()


def test_undo_dry_run_reports_the_selection_and_touches_nothing(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    cover = _sidecar(tmp_path, "a/cover.jpg")
    with changelog.open_log(log) as session:
        session.folder_cover(str(cover), b"art")

    result = changelog.undo(log, dry_run=True)
    assert result.applied == 1 and result.reverted[0]["path"] == str(cover)
    assert cover.exists()  # nothing reverted
    assert len(changelog.list_changes(log)) == 1  # and the log still holds the change


def _cover_then_rename(tmp_path: Path, log: str) -> tuple[Path, Path, str]:
    """A sidecar we wrote, then renamed: the older change now keys on a path that has moved."""
    cover = _sidecar(tmp_path, "cover.jpg")
    moved = tmp_path / "cover-old.jpg"
    with changelog.open_log(log) as session:
        session.folder_cover(str(cover), b"art")
        cover.rename(moved)
        session.rename(str(cover), str(moved))
        return cover, moved, session.run


def test_undo_refuses_a_change_whose_file_a_later_rename_moved(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    cover, moved, _run = _cover_then_rename(tmp_path, log)
    rows = changelog.list_changes(log)
    rename_id = next(r["id"] for r in rows if r["kind"] == "rename")
    cover_id = next(r["id"] for r in rows if r["kind"] == "foldercover")

    result = changelog.undo(log, ids=[cover_id])
    assert result.applied == 0 and rename_id in result.errors[0]
    assert moved.exists() and not cover.exists()  # nothing moved or deleted
    assert len(changelog.list_changes(log)) == 2  # the blocked change stays undo-able


def test_undo_of_the_whole_run_unwinds_the_rename_first(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    cover, moved, run = _cover_then_rename(tmp_path, log)
    # the rename is in the same selection, so it reverts first and the older change is not blocked
    assert changelog.undo(log, run=run).applied == 2
    assert not moved.exists() and not cover.exists()
    assert changelog.list_changes(log) == []


def test_rename_row_shows_the_names_not_two_truncated_paths() -> None:
    row = RenameChange(1.0, "/m/new name.mp3", RenameChange.kind, "/m/old name.mp3", "/m/new name.mp3").row()
    assert (row["old"], row["new"]) == ("old name.mp3", "new name.mp3")


def test_row_truncates_a_base64_cover_payload() -> None:
    row = CoverChange(1.0, "a.mp3", CoverChange.kind, "QUJD" * 200, "itunes").row()
    assert len(row["old"]) == 60 and row["old"].endswith("...")
    assert row["kind"] == "cover"


def test_list_changes_is_newest_first_and_limited(tmp_path: Path) -> None:
    log = str(tmp_path / "c.jsonl")
    with changelog.open_log(log) as session:
        for i in range(3):
            session.folder_cover(str(tmp_path / f"{i}.jpg"), b"art")
    assert [Path(r["path"]).name for r in changelog.list_changes(log)] == ["2.jpg", "1.jpg", "0.jpg"]
    assert len(changelog.list_changes(log, limit=2)) == 2


def test_list_changes_on_a_missing_log_is_empty(tmp_path: Path) -> None:
    assert changelog.list_changes(str(tmp_path / "nope.jsonl")) == []


def test_run_is_omitted_from_a_dict_when_unset() -> None:
    d = {"ts": 1.5, "path": "a.mp3", "field": "title", "old": "A", "new": "B"}
    entry = changelog.entry_from_dict(d)  # a log written before `run` existed
    assert entry.to_dict() == d and entry.row()["run"] == ""
    assert entry_id(entry)  # still addressable


def test_read_log_skips_a_torn_last_line(tmp_path: Path) -> None:
    log = tmp_path / "c.jsonl"
    log.write_text(
        '{"ts": 1, "path": "a.mp3", "field": "title", "old": "A", "new": "B"}\n{"ts": 2, "path": "b", "fie',
        encoding="utf-8",
    )
    entries = changelog.read_log(log)
    assert len(entries) == 1 and entries[0].old == "A" and isinstance(entries[0], ChangeEntry)
