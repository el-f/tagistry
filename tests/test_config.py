"""config: resolve state paths to a stable base dir, never CWD. Env > config.toml > default."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tagistry import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TAGISTRY_DIR", "TAGISTRY_ROOT", "TAGISTRY_LOG", "TAGISTRY_REVIEW"):
        monkeypatch.delenv(var, raising=False)


def test_base_dir_defaults_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.base_dir() == Path.home() / ".tagistry"


def test_base_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    assert config.base_dir() == tmp_path


def test_log_and_review_default_under_base_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    assert config.log_path() == str(tmp_path / "changes.jsonl")
    assert config.review_path() == str(tmp_path / "review.csv")
    assert os.path.isabs(config.log_path())  # never a bare CWD-relative name


def test_env_log_and_review_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("TAGISTRY_LOG", str(tmp_path / "custom.jsonl"))
    monkeypatch.setenv("TAGISTRY_REVIEW", str(tmp_path / "custom.csv"))
    assert config.log_path() == str(tmp_path / "custom.jsonl")
    assert config.review_path() == str(tmp_path / "custom.csv")


def test_config_toml_provides_paths_and_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'root = "D:/Music"\nlog = "D:/state/changes.jsonl"\nreview = "D:/state/review.csv"\n',
        encoding="utf-8",
    )
    assert config.default_root() == str(Path("D:/Music"))
    assert config.log_path() == str(Path("D:/state/changes.jsonl"))
    assert config.review_path() == str(Path("D:/state/review.csv"))


def test_env_wins_over_config_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('log = "D:/from_toml.jsonl"\n', encoding="utf-8")
    monkeypatch.setenv("TAGISTRY_LOG", str(tmp_path / "from_env.jsonl"))
    assert config.log_path() == str(tmp_path / "from_env.jsonl")


def test_malformed_config_toml_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("this is not = valid = toml [[[", encoding="utf-8")
    assert config.log_path() == str(tmp_path / "changes.jsonl")  # falls back to default, no crash


def test_default_root_none_without_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    assert config.default_root() is None


def test_default_root_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_ROOT", str(tmp_path / "Music"))
    assert config.default_root() == str(tmp_path / "Music")


def test_cache_path_under_base_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path))
    assert config.cache_path("lastfm") == str(tmp_path / "cache" / "lastfm")
