"""Assemble the Providers bundle and the layer-2 researcher from CLI flags and on-disk secrets.

Owns one decision: how a run's ground-truth providers are constructed — which credentials are
read, which fall back to a keyless path, and how the researcher decorators stack. Kept out of the
scan/apply pipeline so construction and orchestration change independently. No I/O side effects
beyond reading secrets: warnings are returned, so the CLI and MCP adapters render them their own way.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config
from .providers import AcoustID, MusicBrainz, Providers
from .research import (
    CachingResearcher,
    CliAgentResearcher,
    HttpAgentResearcher,
    MajorityResearcher,
    MBVerifier,
    MusicBrainzVerifiedResearcher,
    NullResearcher,
    Researcher,
)

DEFAULT_RESEARCH_CACHE = config.cache_path("research_cache.json")


def make_researcher(
    kind: str, mb: MBVerifier | None, cache: str | Path | None = None, timeout: int = 120
) -> Researcher:
    """Build a layer-2 researcher from a flag, cross-verified against MusicBrainz and cached:
    'cli' runs an agent CLI (`claude -p`, no key); 'http' calls an LLM HTTP API; 'majority' votes
    cli + http (both must agree); 'none' declines everything (the offline default)."""
    if kind in ("", "none"):
        return NullResearcher()
    base = _base_researcher(kind, timeout)
    if mb is not None:
        base = MusicBrainzVerifiedResearcher(base, mb)
    if cache:
        base = CachingResearcher(base, cache)
    return base


def _base_researcher(kind: str, timeout: int) -> Researcher:
    if kind == "cli":
        return CliAgentResearcher(timeout=timeout)
    if kind == "http":
        return _http_researcher()
    if kind == "majority":
        # Two different adapters must agree, which guards one of them being confidently wrong
        return MajorityResearcher([CliAgentResearcher(timeout=timeout), _http_researcher()], min_votes=2)
    raise ValueError(f"unknown researcher: {kind!r} (use 'cli', 'http', 'majority', or 'none')")


def _http_researcher() -> HttpAgentResearcher:
    """An HTTP-API researcher from env: $RESEARCHER_BASE_URL (default Anthropic), $RESEARCHER_MODEL,
    and the key in $RESEARCHER_API_KEY / ~/.researcher_key. The wire (Anthropic vs OpenAI) is
    detected from the base URL. Raises if the model or key is missing (surfaced as a warning)."""
    base_url = os.environ.get("RESEARCHER_BASE_URL", "https://api.anthropic.com/v1")
    model = os.environ.get("RESEARCHER_MODEL")
    api_key = _secret("RESEARCHER_API_KEY", ".researcher_key")
    if not model or not api_key:
        raise ValueError("http researcher needs $RESEARCHER_MODEL and $RESEARCHER_API_KEY (or ~/.researcher_key)")
    return HttpAgentResearcher(base_url, model, api_key)


def _secret(env_var: str, filename: str) -> str | None:
    """A secret from $<env_var>, then ~/<filename>, or None. Shared by the last.fm key and Discogs
    token so a new provider credential is one call, not a copy of the read-both-places logic."""
    env = os.environ.get(env_var)
    if env and env.strip():
        return env.strip()
    path = Path.home() / filename
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return None


def build_providers(
    online: bool,
    fingerprint: bool,
    researcher: str = "none",
    lastfm: bool = False,
    discogs: bool = False,
    researcher_timeout: int = 120,
) -> tuple[Providers, list[str]]:
    """Assemble providers from flags. Returns (providers, warnings) — no I/O side effects,
    so both the CLI and MCP adapters can render warnings their own way."""
    providers = Providers()
    warnings: list[str] = []
    if online:
        providers.musicbrainz = MusicBrainz()
    if fingerprint:
        try:
            providers.acoustid = AcoustID()
        except Exception as exc:
            warnings.append(f"acoustid unavailable, skipping blank_id: {exc}")
    if lastfm:
        api_key = _secret("LASTFM_KEY", ".lastfm_key")  # not 'key' -- that shadows the imported text.key()
        if api_key:
            from .providers.lastfm import default_lastfm

            providers.lastfm = default_lastfm(api_key, config.cache_path("lastfm_cache"))
        else:  # no key -> keyless public-page scrape (works out of the box, brittle to HTML changes)
            from .providers.lastfm import default_lastfm_page

            providers.lastfm = default_lastfm_page(config.cache_path("lastfm_page_cache"))
            warnings.append(
                "no last.fm key -- using the keyless page scraper (add $LASTFM_KEY / ~/.lastfm_key for the JSON API)"
            )
    if discogs:
        token = _secret("DISCOGS_TOKEN", ".discogs_token")
        if token:
            from .providers.discogs import default_discogs

            providers.discogs = default_discogs(token, config.cache_path("discogs_cache"))
        else:
            warnings.append("no Discogs token -- genre_fill is off (set $DISCOGS_TOKEN / ~/.discogs_token)")
    try:
        providers.researcher = make_researcher(
            researcher, providers.musicbrainz, DEFAULT_RESEARCH_CACHE, researcher_timeout
        )
    except ValueError as exc:
        warnings.append(str(exc))
    return providers, warnings
