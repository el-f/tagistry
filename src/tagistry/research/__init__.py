"""Layer-2 residue resolver: a pluggable agent/web adapter, called ONLY when the
deterministic checks return uncertain (cross-script transliteration, obscure acts).

Vendor-neutral: the protocol (models.Researcher) is the contract; the concrete agent
(a CLI like `claude -p`, an HTTP LLM API, or a chain of them) is a swappable backend.
An answer MUST cite sources and MAY decline ("uncertain") — it is never forced to guess.
The default NullResearcher always declines, so nothing changes until a backend is wired.

Layout: models (types + protocols), prompt (wire format), backends (CLI + HTTP adapters),
decorators (caching / majority vote / MusicBrainz cross-verify, composable over any Researcher).
"""

from __future__ import annotations

from .backends import CliAgentResearcher, HttpAgentResearcher
from .decorators import CachingResearcher, MajorityResearcher, MusicBrainzVerifiedResearcher
from .models import (
    MBVerifier,
    NullResearcher,
    ResearchAnswer,
    Researcher,
    ResearcherError,
    ResearchQuestion,
)
from .prompt import build_research_prompt, parse_answer

__all__ = [
    "CachingResearcher",
    "CliAgentResearcher",
    "HttpAgentResearcher",
    "MBVerifier",
    "MajorityResearcher",
    "MusicBrainzVerifiedResearcher",
    "NullResearcher",
    "ResearchAnswer",
    "ResearchQuestion",
    "Researcher",
    "ResearcherError",
    "build_research_prompt",
    "parse_answer",
]
