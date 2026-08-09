"""Resolve Tagistry's on-disk paths to one stable location, never CWD.

A bare filename would put the change log -- the ONLY undo net -- in whatever directory the run
started in, where it is invisible and un-mergeable. Everything that must survive a run (the
change log, the review CSV, the provider caches) resolves through here to one base dir.

Precedence for every path: an explicit env var, then a `config.toml` in the base dir,
then a default under the base dir. An explicit CLI argument still wins over all of these
(the CLI passes its own value instead of the default).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

_ENV_DIR = "TAGISTRY_DIR"
_ENV_ROOT = "TAGISTRY_ROOT"
_ENV_LOG = "TAGISTRY_LOG"
_ENV_REVIEW = "TAGISTRY_REVIEW"
_CONFIG_NAME = "config.toml"


def base_dir() -> Path:
    """The directory holding Tagistry's state: change log, review CSV, provider caches.
    $TAGISTRY_DIR, else ~/.tagistry. Never CWD -- a run's undo net must not depend
    on where it was launched."""
    env = os.environ.get(_ENV_DIR)
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".tagistry"


def _config() -> dict[str, object]:
    """The optional config.toml in the base dir. Missing / unreadable / malformed -> {}."""
    path = base_dir() / _CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _resolve(env_key: str, toml_key: str, default_name: str) -> str:
    """An absolute path from the env var, then config.toml, then <base_dir>/<default_name>."""
    env = os.environ.get(env_key)
    if env and env.strip():
        return str(Path(env).expanduser())
    val = _config().get(toml_key)
    if isinstance(val, str) and val.strip():
        return str(Path(val).expanduser())
    return str(base_dir() / default_name)


def log_path() -> str:
    """Absolute path of the change log (the undo source)."""
    return _resolve(_ENV_LOG, "log", "changes.jsonl")


def review_path() -> str:
    """Absolute path of the default review CSV."""
    return _resolve(_ENV_REVIEW, "review", "review.csv")


def default_root() -> str | None:
    """The library root from $TAGISTRY_ROOT or config.toml, or None if neither is set.
    A CLI `root` argument overrides this; commands fall back to it when the argument is omitted."""
    env = os.environ.get(_ENV_ROOT)
    if env and env.strip():
        return str(Path(env).expanduser())
    val = _config().get("root")
    return str(Path(val).expanduser()) if isinstance(val, str) and val.strip() else None


def cache_path(name: str) -> str:
    """Absolute path for a provider cache file under <base_dir>/cache/, so caches never
    scatter into CWD. The parent dir is created lazily by the session/cache factory."""
    return str(base_dir() / "cache" / name)
