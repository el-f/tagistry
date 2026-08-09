"""Composable decorators over any Researcher: persistent cache, majority vote, MusicBrainz
cross-verify. Each adds one capability and hides its own implementation, so a new backend is one
adapter (backends.py) and a new policy is one decorator here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from ..atomicio import atomic_write
from ..text import alnum, key, subset
from .models import MBVerifier, ResearchAnswer, Researcher, ResearchQuestion
from .prompt import as_confidence, as_sources


class CachingResearcher:
    """Persist answers to a JSON file keyed by the question, so a repeat question is free.
    Read-through; the cache survives across runs and process restarts."""

    def __init__(self, inner: Researcher, cache_path: str | Path) -> None:
        self._inner = inner
        self._path = Path(cache_path)
        self._cache: dict[str, dict[str, object]] = self._load()

    def _load(self) -> dict[str, dict[str, object]]:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}
        return {}

    def resolve(self, question: ResearchQuestion) -> ResearchAnswer:
        k = json.dumps({"kind": question.kind, "ask": question.ask, "ctx": question.context}, sort_keys=True)
        cached = self._cache.get(k)
        if cached is not None:
            return _answer_from_dict(cached)
        ans = self._inner.resolve(question)
        # An 'uncertain' may be a transient miss; caching it would poison every later run
        if ans.decision != "uncertain":
            self._cache[k] = _answer_to_dict(ans)
            self._save()
        return ans

    def _save(self) -> None:
        # A plain '.tmp' lets two runs sharing the cache clash, and leaves a partial file on a crash
        atomic_write(self._path, lambda fh: fh.write(json.dumps(self._cache, ensure_ascii=False)))


class MajorityResearcher:
    """Ask several researchers; return a value only when >= min_votes usable answers AGREE
    on it (exact match key). Diversity guards against one adapter's confident-wrong answer."""

    def __init__(self, researchers: Iterable[Researcher], min_votes: int = 2) -> None:
        self._researchers = list(researchers)
        self._min = min_votes

    def resolve(self, question: ResearchQuestion) -> ResearchAnswer:
        usable = [a for a in (r.resolve(question) for r in self._researchers) if a.is_usable and a.value]
        if not usable:
            return ResearchAnswer(decision="uncertain")
        groups: dict[str, list[ResearchAnswer]] = {}
        for a in usable:
            groups.setdefault(key(a.value or ""), []).append(a)
        best = max(groups.values(), key=lambda g: (len(g), sum(x.confidence for x in g)))
        if len(best) < self._min:
            return ResearchAnswer(decision="uncertain")
        sources = tuple(dict.fromkeys(s for a in best for s in a.sources))
        conf = sum(x.confidence for x in best) / len(best)
        return replace(
            best[0], confidence=conf, sources=sources, reasoning=f"{len(best)}/{len(self._researchers)} agree"
        )


class MusicBrainzVerifiedResearcher:
    """Cross-check the inner researcher's answer against MusicBrainz: the resolved artist must
    match the credited artist of the title's top recording, or be a real MB artist. An answer MB
    can't corroborate is downgraded to 'uncertain' — the agent can be confidently wrong."""

    def __init__(self, inner: Researcher, mb: MBVerifier) -> None:
        self._inner = inner
        self._mb = mb

    def resolve(self, question: ResearchQuestion) -> ResearchAnswer:
        ans = self._inner.resolve(question)
        if not ans.is_usable or not ans.value:
            return ans
        if self._corroborated(ans.value, question.context.get("title", "")):
            return ans
        return replace(ans, decision="uncertain", reasoning=f"MB uncorroborated: {ans.reasoning}")

    def _corroborated(self, value: str, title: str) -> bool:
        if title:
            top = self._mb.recording_top(title)
            if top and top[2] >= 90 and (alnum(value) == alnum(top[1]) or subset(value, top[1])):
                return True
        name, score = self._mb.artist_search(value)
        return score >= 90 and alnum(name) == alnum(value)


def _answer_to_dict(a: ResearchAnswer) -> dict[str, object]:
    return {
        "decision": a.decision,
        "value": a.value,
        "confidence": a.confidence,
        "sources": list(a.sources),
        "reasoning": a.reasoning,
    }


def _answer_from_dict(d: dict[str, object]) -> ResearchAnswer:
    return ResearchAnswer(
        decision=str(d.get("decision", "uncertain")),
        value=(str(d["value"]) if d.get("value") not in (None, "") else None),
        confidence=as_confidence(d.get("confidence")),
        sources=as_sources(d.get("sources")),
        reasoning=str(d.get("reasoning", "")),
    )
