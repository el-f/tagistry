"""--rename: album-folder-aware, filesystem-safe, undo restores the old name."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mediafile import MediaFile

from tagistry import changelog, pipeline, tagio
from tagistry.domain import FileLockedError
from tagistry.text import safe_filename

FIXTURES = Path(__file__).parent / "fixtures"


def _song(path: Path, **tags: str) -> str:
    shutil.copy2(FIXTURES / "sample.mp3", path)
    mf = MediaFile(str(path))
    for k, v in tags.items():
        setattr(mf, k, v)
    mf.save()
    return str(path)


# --- safe_filename ----------------------------------------------------------


def test_safe_filename_strips_illegal_keeps_accents() -> None:
    assert safe_filename("AC/DC - Thunderstruck") == "AC-DC - Thunderstruck"
    assert safe_filename('Song: The "Best"?') == "Song- The Best"
    assert safe_filename("Beyoncé - Déjà Vu") == "Beyoncé - Déjà Vu"  # accents kept
    assert safe_filename("Trailing dot.") == "Trailing dot"  # Windows: no trailing dot
    assert safe_filename("a\\b|c*d") == "a-b-cd"  # \ and | -> dash, * dropped


# --- tagio.rename_file ------------------------------------------------------


def test_rename_file_moves_and_is_noop_on_same_path(tmp_path: Path) -> None:
    old = _song(tmp_path / "old.mp3", artist="A", title="B")
    new = str(tmp_path / "new.mp3")
    tagio.rename_file(old, new)
    assert Path(new).exists() and not Path(old).exists()
    tagio.rename_file(new, new)  # no-op, no error


def test_rename_file_refuses_to_overwrite(tmp_path: Path) -> None:
    a = _song(tmp_path / "a.mp3", title="A")
    b = _song(tmp_path / "b.mp3", title="B")
    with pytest.raises(FileExistsError):
        tagio.rename_file(a, b)
    assert Path(a).exists() and MediaFile(b).title == "B"  # both intact


def test_rename_file_locked_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _song(tmp_path / "o.mp3", title="X")
    new = str(tmp_path / "n.mp3")

    def locked(*a: object, **k: object) -> None:
        raise PermissionError("player holds it")

    monkeypatch.setattr("tagistry.tagio.os.rename", locked)
    with pytest.raises(FileLockedError):
        tagio.rename_file(old, new)


# --- album-folder classification --------------------------------------------


def test_album_folder_detected_by_shared_album_and_tracknums(tmp_path: Path) -> None:
    album = tmp_path / "Some Album"
    album.mkdir()
    for i, t in enumerate(["One", "Two", "Three"], 1):
        _song(album / f"{i:02d} {t}.mp3", artist="The Band", title=t, album="Some Album")
    tracks = pipeline.read_tracks(str(album))
    assert str(album) in pipeline.classify_album_folders(tracks)


def test_loose_folder_not_an_album(tmp_path: Path) -> None:
    loose = tmp_path / "Playlist"
    loose.mkdir()
    _song(loose / "Radiohead - Creep.mp3", artist="Radiohead", title="Creep")
    _song(loose / "Nirvana - Lithium.mp3", artist="Nirvana", title="Lithium")
    tracks = pipeline.read_tracks(str(loose))
    assert str(loose) not in pipeline.classify_album_folders(tracks)


# --- rename_plan ------------------------------------------------------------


def test_rename_plan_skips_album_folders_by_default(tmp_path: Path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    _song(album / "01 One.mp3", artist="Band", title="One", album="Album")
    _song(album / "02 Two.mp3", artist="Band", title="Two", album="Album")
    loose = tmp_path / "Loose"
    loose.mkdir()
    weird = _song(loose / "weird_name.mp3", artist="Solo", title="Hit")

    tracks = pipeline.read_tracks(str(tmp_path))
    plan = pipeline.rename_plan(tracks)
    olds = {old for old, _ in plan}
    assert weird in olds and not any("Album" in old for old in olds)


def test_rename_all_includes_album_folders(tmp_path: Path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    _song(album / "01 One.mp3", artist="Band", title="One", album="Album")
    _song(album / "02 Two.mp3", artist="Band", title="Two", album="Album")
    tracks = pipeline.read_tracks(str(album))
    assert len(pipeline.rename_plan(tracks, rename_all=True)) == 2


def test_rename_plan_targets_artist_dash_title(tmp_path: Path) -> None:
    _song(tmp_path / "junk.mp3", artist="Daft Punk", title="One More Time")
    (plan,) = pipeline.rename_plan(pipeline.read_tracks(str(tmp_path)))
    assert Path(plan[1]).name == "Daft Punk - One More Time.mp3"


def test_rename_plan_skips_collision(tmp_path: Path) -> None:
    _song(tmp_path / "Daft Punk - One More Time.mp3", artist="X", title="Y", album="")  # target exists
    _song(tmp_path / "junk.mp3", artist="Daft Punk", title="One More Time")
    # both files are loose; 'junk' would collide with the existing correctly-named file
    plan = pipeline.rename_plan(pipeline.read_tracks(str(tmp_path)))
    assert all(Path(new).name != "Daft Punk - One More Time.mp3" or old == new for old, new in plan)


def test_rename_plan_skips_blank_artist_or_title(tmp_path: Path) -> None:
    _song(tmp_path / "a.mp3", artist="", title="Orphan")
    assert pipeline.rename_plan(pipeline.read_tracks(str(tmp_path))) == []


# --- apply + undo -----------------------------------------------------------


def test_apply_renames_then_undo_restores_name(tmp_path: Path) -> None:
    old = _song(tmp_path / "bad name.mp3", artist="Muse", title="Hysteria")
    log = str(tmp_path / "c.jsonl")
    plan = pipeline.rename_plan(pipeline.read_tracks(str(tmp_path)))
    result = pipeline.apply_renames(plan, log)
    assert result.applied == 1
    new = str(tmp_path / "Muse - Hysteria.mp3")
    assert Path(new).exists() and not Path(old).exists()

    assert pipeline.status(log)["applied_changes"] == 1
    pipeline.undo(log, 1)
    assert Path(old).exists() and not Path(new).exists()


def test_apply_renames_reports_a_log_write_failure_but_keeps_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rename is on disk but the undo-log append failed: count it and surface it, never a silent abort.
    old = _song(tmp_path / "bad name.mp3", artist="Muse", title="Hysteria")
    log = str(tmp_path / "c.jsonl")
    plan = pipeline.rename_plan(pipeline.read_tracks(str(tmp_path)))

    def boom(self: object, old_path: str, new_path: str) -> None:
        raise OSError("disk full while appending to the undo log")

    monkeypatch.setattr(changelog.LogSession, "rename", boom)
    result = pipeline.apply_renames(plan, log)
    assert result.applied == 1
    assert any("NOT logged" in e for e in result.errors)
    assert Path(tmp_path / "Muse - Hysteria.mp3").exists() and not Path(old).exists()


def test_rename_plan_stage_round_trip(tmp_path: Path) -> None:
    # --stage writes a reviewable plan CSV and renames NOTHING; apply-renames then applies it
    old = _song(tmp_path / "bad name.mp3", artist="Muse", title="Hysteria")
    plan_csv = str(tmp_path / "plan.csv")
    log = str(tmp_path / "c.jsonl")
    plan = pipeline.rename_plan(pipeline.read_tracks(str(tmp_path)))

    n = pipeline.write_rename_plan(plan, plan_csv)
    assert n == 1 and Path(old).exists()  # staging touches nothing on disk

    pairs = pipeline.read_rename_plan(plan_csv)
    assert pairs == plan
    result = pipeline.apply_renames(pairs, log)
    new = str(tmp_path / "Muse - Hysteria.mp3")
    assert result.applied == 1 and Path(new).exists() and not Path(old).exists()


def test_read_rename_plan_rejects_a_missing_column(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("old_path,reason\na.mp3,x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        pipeline.read_rename_plan(str(bad))


def test_apply_renames_dry_run_writes_nothing(tmp_path: Path) -> None:
    old = _song(tmp_path / "x.mp3", artist="A", title="B")
    log = str(tmp_path / "c.jsonl")
    plan = pipeline.rename_plan(pipeline.read_tracks(str(tmp_path)))
    result = pipeline.apply_renames(plan, log, dry_run=True)
    assert result.applied == 1 and Path(old).exists() and not Path(log).exists()
