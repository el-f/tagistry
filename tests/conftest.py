"""Shared fixtures: a Track factory and a temp copy of a real audio sample."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from tagistry.domain import Track

FIXTURES = Path(__file__).parent / "fixtures"


def make_track(path: str = "x.mp3", ext: str = "mp3", **tags: str) -> Track:
    base = {"artist": "", "title": "", "album": "", "albumartist": ""}
    base.update(tags)
    return Track(path=path, ext=ext, tags=base)


@pytest.fixture
def track_factory() -> Callable[..., Track]:
    return make_track


@pytest.fixture(params=["sample.mp3", "sample.opus", "sample.flac"])
def audio_file(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A writable copy of a tiny ffmpeg-generated audio sample (~0.3s, no tags), per format."""
    name = str(request.param)
    dst = tmp_path / name
    shutil.copy2(FIXTURES / name, dst)
    return dst
