"""Cover art: Cover Art Archive -> iTunes cascade, cross-format embed, undo removes it."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

from tagistry import covers, pipeline, tagio
from tagistry.providers.coverart import CoverArtArchive, CoverArtFetcher, ITunes, _looks_like_image

FIXTURES = Path(__file__).parent / "fixtures"

# a tiny valid 1x1 PNG — mediafile derives the mime type from the magic bytes
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20  # JPEG magic


def test_looks_like_image_rejects_html() -> None:
    assert _looks_like_image(PNG) and _looks_like_image(JPEG)
    assert not _looks_like_image(b"<!DOCTYPE html><html>rate limited</html>")  # a 200 error page
    assert not _looks_like_image(b"")


# --- Cover Art Archive ------------------------------------------------------


def test_caa_front_returns_bytes() -> None:
    def image_getter(url: str) -> bytes | None:
        assert "coverartarchive.org/release/mbid-1/front" in url
        return PNG

    assert CoverArtArchive(image_getter).front("mbid-1") == PNG


def test_caa_front_none_on_miss() -> None:
    assert CoverArtArchive(lambda url: None).front("nope") is None


# --- iTunes -----------------------------------------------------------------


def test_itunes_upscales_and_fetches() -> None:
    seen: dict[str, str] = {}

    def json_getter(url: str) -> dict[str, object] | None:
        seen["search"] = url
        return {"resultCount": 1, "results": [{"artworkUrl100": "https://is1.mzstatic.com/x/100x100bb.jpg"}]}

    def image_getter(url: str) -> bytes | None:
        seen["art"] = url
        return JPEG

    out = ITunes(json_getter, image_getter).front("Daft Punk", "One More Time")
    assert out == JPEG
    assert "600x600" in seen["art"]  # upscaled from 100x100
    assert "Daft" in seen["search"]


def test_itunes_none_on_no_results() -> None:
    assert ITunes(lambda url: {"resultCount": 0, "results": []}, lambda url: JPEG).front("x", "y") is None


# --- cascade ----------------------------------------------------------------


def test_fetcher_prefers_caa_then_falls_back_to_itunes() -> None:
    caa_hit = CoverArtFetcher(CoverArtArchive(lambda url: PNG), ITunes(lambda u: None, lambda u: None))
    got = caa_hit.fetch("A", "B", release_mbid="rel-1")
    assert got is not None and got[0] == PNG and got[1] == "coverartarchive"

    itunes_only = CoverArtFetcher(
        CoverArtArchive(lambda url: None),
        ITunes(lambda u: {"resultCount": 1, "results": [{"artworkUrl100": "http://x/100x100.jpg"}]}, lambda u: JPEG),
    )
    got = itunes_only.fetch("A", "B", release_mbid="rel-1")
    assert got is not None and got[0] == JPEG and got[1] == "itunes"

    miss = CoverArtFetcher(CoverArtArchive(lambda url: None), ITunes(lambda u: {"results": []}, lambda u: None))
    assert miss.fetch("A", "B") is None


# --- embed across formats + undo --------------------------------------------


def test_embed_read_clear_round_trip(audio_file: Path) -> None:
    assert not tagio.read_images(str(audio_file))  # starts art-less
    tagio.set_front_image(str(audio_file), PNG)
    imgs = tagio.read_images(str(audio_file))
    assert imgs and imgs[0].data == PNG
    tagio.clear_images(str(audio_file))
    assert not tagio.read_images(str(audio_file))


class _Fetcher:
    def __init__(self, data: bytes | None) -> None:
        self._data = data

    def fetch(self, artist: str, title: str, release_mbid: str | None = None) -> tuple[bytes, str] | None:
        return (self._data, "itunes") if self._data else None


def _song(dir_: Path, name: str, **tags: str) -> str:
    from mediafile import MediaFile

    path = dir_ / name
    shutil.copy2(FIXTURES / "sample.mp3", path)
    mf = MediaFile(str(path))
    for k, v in tags.items():
        setattr(mf, k, v)
    mf.save()
    return str(path)


def test_embed_covers_adds_and_undo_removes(tmp_path: Path) -> None:
    path = _song(tmp_path, "a.mp3", artist="Muse", title="Hysteria")
    log = str(tmp_path / "c.jsonl")
    result = covers.embed_covers(tmp_path.as_posix(), _Fetcher(PNG), log)
    assert result.applied == 1 and tagio.read_images(path)

    assert pipeline.status(log)["applied_changes"] == 1
    pipeline.undo(log, 1)
    assert not tagio.read_images(path)  # undo removed the art we added


def test_embed_replace_undo_restores_original_art(tmp_path: Path) -> None:
    # replacing existing art must be undo-safe: undo restores the ORIGINAL, never wipes it
    path = _song(tmp_path, "a.mp3", artist="Muse", title="Hysteria")
    tagio.set_front_image(path, JPEG)  # user's own art
    log = str(tmp_path / "c.jsonl")
    result = covers.embed_covers(tmp_path.as_posix(), _Fetcher(PNG), log, replace=True)
    assert result.applied == 1 and tagio.read_images(path)[0].data == PNG  # replaced
    pipeline.undo(log, 1)
    assert tagio.read_images(path)[0].data == JPEG  # original restored, not cleared


def test_embed_covers_skips_files_with_art(tmp_path: Path) -> None:
    path = _song(tmp_path, "a.mp3", artist="Muse", title="Hysteria")
    tagio.set_front_image(path, JPEG)  # already has art
    log = str(tmp_path / "c.jsonl")
    result = covers.embed_covers(tmp_path.as_posix(), _Fetcher(PNG), log)
    assert result.applied == 0 and result.skipped == 1
    assert tagio.read_images(path)[0].data == JPEG  # untouched


def test_embed_covers_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = _song(tmp_path, "a.mp3", artist="A", title="B")
    log = str(tmp_path / "c.jsonl")
    result = covers.embed_covers(tmp_path.as_posix(), _Fetcher(PNG), log, dry_run=True)
    assert result.applied == 1 and not tagio.read_images(path) and not Path(log).exists()


# --- folder cover.jpg (disk-cheap default) ----------------------------------


def test_folder_cover_written_once_and_undo_deletes(tmp_path: Path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    _song(album, "01.mp3", artist="Band", title="One", album="Debut")
    _song(album, "02.mp3", artist="Band", title="Two", album="Debut")
    log = str(tmp_path / "c.jsonl")
    result = covers.save_folder_covers(tmp_path.as_posix(), _Fetcher(PNG), log)
    cover = album / "cover.png"  # PNG magic -> .png
    assert result.applied == 1 and cover.exists() and cover.read_bytes() == PNG

    pipeline.undo(log, 1)
    assert not cover.exists()  # undo deletes the sidecar


def test_folder_cover_skips_folder_with_existing_cover(tmp_path: Path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    _song(album, "01.mp3", artist="Band", title="One", album="Debut")
    (album / "cover.jpg").write_bytes(JPEG)  # already has one
    log = str(tmp_path / "c.jsonl")
    result = covers.save_folder_covers(tmp_path.as_posix(), _Fetcher(PNG), log)
    assert result.applied == 0 and result.skipped == 1
    assert (album / "cover.jpg").read_bytes() == JPEG  # untouched
