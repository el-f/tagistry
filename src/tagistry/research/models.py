"""Core research types: the question/answer values and the Researcher/MBVerifier contracts.

Pure data + protocols, no I/O. The concrete backends (backends.py) and the composable
decorators (decorators.py) both depend on these, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    kind: str  # "artist_for_title" | "is_collaboration" | "is_soundtrack" ...
    ask: str
    context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResearchAnswer:
    decision: str  # a kind-specific verdict, or "uncertain"
    value: str | None = None  # the resolved artist/title, if any
    confidence: float = 0.0
    sources: tuple[str, ...] = ()  # REQUIRED for a non-uncertain answer
    reasoning: str = ""

    @property
    def is_usable(self) -> bool:
        """Trust an answer only if it decided, is confident, and cited a source."""
        return self.decision != "uncertain" and self.confidence >= 0.8 and bool(self.sources)


@runtime_checkable
class Researcher(Protocol):
    def resolve(self, question: ResearchQuestion) -> ResearchAnswer: ...


@runtime_checkable
class MBVerifier(Protocol):
    def recording_top(self, title: str, artist: str = "") -> tuple[str, str, int] | None: ...
    def artist_search(self, query: str) -> tuple[str, int]: ...


class ResearcherError(Exception):
    """A terminal, non-retryable researcher failure (bad API key, billing, forbidden). Surfaced
    loudly instead of being disguised as a normal 'uncertain', so a misconfigured run isn't silent."""


class NullResearcher:
    """Default: always declines. Layer 1 (deterministic checks) does all the work."""

    def resolve(self, question: ResearchQuestion) -> ResearchAnswer:
        return ResearchAnswer(decision="uncertain")
