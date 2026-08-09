"""The researcher wiring: build a researcher from a flag, and run a disambiguate
pass that lets the agent confirm/correct REVIEW rows in a staged CSV."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from fakes import FakeMusicBrainz
from tagistry import pipeline
from tagistry.providers import NullResearcher, Providers
from tagistry.research import (
    CachingResearcher,
    CliAgentResearcher,
    HttpAgentResearcher,
    MajorityResearcher,
    MusicBrainzVerifiedResearcher,
    ResearchAnswer,
    ResearchQuestion,
)


def _review(tmp_path: Path, *rows: list[str]) -> str:
    p = tmp_path / "r.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(pipeline.REVIEW_HEADER)
        for r in rows:
            w.writerow(r)
    return str(p)


class _FixedResearcher:
    def __init__(self, answer: ResearchAnswer) -> None:
        self._a = answer

    def resolve(self, q: ResearchQuestion) -> ResearchAnswer:
        return self._a


# --- make_researcher --------------------------------------------------------


def test_make_researcher_none_is_null() -> None:
    assert isinstance(pipeline.make_researcher("none", None), NullResearcher)


def test_make_researcher_cli_wraps_mb_verify_and_cache(tmp_path: Path) -> None:
    mb = FakeMusicBrainz()
    r = pipeline.make_researcher("cli", mb, cache=tmp_path / "cache.json")
    assert isinstance(r, CachingResearcher)
    inner = r._inner
    assert isinstance(inner, MusicBrainzVerifiedResearcher)
    assert isinstance(inner._inner, CliAgentResearcher)


def test_make_researcher_cli_without_mb_is_bare_cli() -> None:
    r = pipeline.make_researcher("cli", None)
    assert isinstance(r, CliAgentResearcher)


def test_make_researcher_cli_wires_timeout() -> None:
    # The --timeout (and the MCP timeout kwarg) must reach the CLI researcher's subprocess timeout.
    r = pipeline.make_researcher("cli", None, timeout=37)
    assert isinstance(r, CliAgentResearcher)
    assert r._timeout == 37


def test_make_researcher_http_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RESEARCHER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("RESEARCHER_API_KEY", "sk-test")
    mb = FakeMusicBrainz()
    r = pipeline.make_researcher("http", mb, cache=tmp_path / "c.json")
    assert isinstance(r, CachingResearcher)
    inner = r._inner
    assert isinstance(inner, MusicBrainzVerifiedResearcher)
    assert isinstance(inner._inner, HttpAgentResearcher)


def test_make_researcher_majority_votes_cli_and_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCHER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("RESEARCHER_API_KEY", "sk-test")
    r = pipeline.make_researcher("majority", None)
    assert isinstance(r, MajorityResearcher)
    voters = r._researchers
    assert [type(v) for v in voters] == [CliAgentResearcher, HttpAgentResearcher]


def test_make_researcher_http_without_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEARCHER_MODEL", raising=False)
    monkeypatch.delenv("RESEARCHER_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/no-such-home-dir"))  # no ~/.researcher_key
    with pytest.raises(ValueError, match="http researcher needs"):
        pipeline.make_researcher("http", None)


def test_make_researcher_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown researcher"):
        pipeline.make_researcher("gpt", None)


# --- disambiguate -----------------------------------------------------------


def test_disambiguate_confirm_applies_when_policy_allows(tmp_path: Path) -> None:
    # an accent-add is policy 'apply' -> a confirmed proposal flips to apply
    review = _review(
        tmp_path,
        ["skip", "resolve_artist", "REVIEW", "x.mp3", "artist", "Beyonce", "Beyoncé", "ev"],
    )
    ans = ResearchAnswer("confirm", "Beyoncé", 0.95, ("https://mb.org/x",), "canonical accent")
    providers = Providers(researcher=_FixedResearcher(ans))
    touched = pipeline.disambiguate(review, providers)
    row = pipeline.read_review(review)[0]
    assert touched == 1 and row.apply == "apply" and "researcher" in row.evidence


def test_disambiguate_confirm_blocked_when_policy_flags(tmp_path: Path) -> None:
    # A whole-value rewrite is policy 'flag': agent confirmation must stage it, never auto-apply.
    review = _review(
        tmp_path,
        ["skip", "resolve_artist", "REVIEW", "x.mp3", "artist", "Lilo & Stitch", "Mark Keali'i Ho'omalu", "ev"],
    )
    ans = ResearchAnswer("confirm", "Mark Keali'i Ho'omalu", 0.95, ("https://mb.org/x",), "chant composer")
    providers = Providers(researcher=_FixedResearcher(ans))
    touched = pipeline.disambiguate(review, providers)
    row = pipeline.read_review(review)[0]
    assert touched == 1 and row.apply == "skip" and "policy" in row.evidence.lower()


def test_disambiguate_leaves_canonicalize_rows_for_the_fingerprint_flow(tmp_path: Path) -> None:
    # Fingerprint retitles are decided by shazam-filter + adjudicate, never by one agent confirmation.
    review = _review(
        tmp_path,
        ["skip", "canonicalize", "REVIEW", "x.mp3", "title", "Clocks", "Clocks (radio edit)", "fp"],
    )
    ans = ResearchAnswer("confirm", "Clocks (radio edit)", 0.99, ("https://mb.org/x",), "would confirm anything")
    providers = Providers(researcher=_FixedResearcher(ans))
    touched = pipeline.disambiguate(review, providers)
    row = pipeline.read_review(review)[0]
    assert touched == 0 and row.apply == "skip"


def test_disambiguate_correction_updates_proposed_stays_skip(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        ["skip", "resolve_artist", "REVIEW", "x.mp3", "artist", "Junk", "Wrong Guess", "ev"],
    )
    ans = ResearchAnswer("artist_for_title", "Correct Artist", 0.9, ("https://mb.org/y",), "actually this")
    providers = Providers(researcher=_FixedResearcher(ans))
    touched = pipeline.disambiguate(review, providers)
    row = pipeline.read_review(review)[0]
    assert touched == 1 and row.proposed == "Correct Artist" and row.apply == "skip"


def test_disambiguate_uncertain_leaves_row_untouched(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        ["skip", "resolve_artist", "REVIEW", "x.mp3", "artist", "Junk", "Guess", "ev"],
    )
    providers = Providers(researcher=NullResearcher())
    touched = pipeline.disambiguate(review, providers)
    row = pipeline.read_review(review)[0]
    assert touched == 0 and row.apply == "skip" and row.proposed == "Guess"


def test_disambiguate_skips_high_and_already_apply_rows(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        ["apply", "title_junk", "HIGH", "x.mp3", "title", "Song (Remastered)", "Song", "ev"],
    )
    # a researcher that would confirm anything — but HIGH rows must not be asked
    ans = ResearchAnswer("confirm", "Song", 0.99, ("https://mb.org/z",), "x")
    providers = Providers(researcher=_FixedResearcher(ans))
    assert pipeline.disambiguate(review, providers) == 0


def test_disambiguate_reports_progress_per_researched_row(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        ["skip", "resolve_artist", "REVIEW", "x.mp3", "artist", "Beyonce", "Beyoncé", "ev"],  # researched
        ["apply", "title_junk", "HIGH", "y.mp3", "title", "A", "B", "ev"],  # HIGH -> skipped, not researched
        ["skip", "resolve_artist", "REVIEW", "z.mp3", "artist", "A", "B", "ev"],  # researched
    )
    ans = ResearchAnswer("confirm", "Beyoncé", 0.9, ("https://mb.org/b",), "ok")
    seen: list[tuple[int, int, str]] = []
    pipeline.disambiguate(
        review, Providers(researcher=_FixedResearcher(ans)), progress=lambda d, t, p: seen.append((d, t, p))
    )
    assert [d for d, _t, _p in seen] == [1, 2]  # only the 2 REVIEW rows are researched, in order
    assert all(t == 2 for _d, t, _p in seen) and [p for _d, _t, p in seen] == ["x.mp3", "z.mp3"]


def test_cli_researcher_warns_on_timeout(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    import subprocess

    from tagistry.research import CliAgentResearcher

    def timing_out(cmd: list[str], prompt: str) -> str:
        raise subprocess.TimeoutExpired(cmd, 1)

    r = CliAgentResearcher(runner=timing_out)
    with caplog.at_level(logging.WARNING):
        ans = r.resolve(ResearchQuestion("verify_proposal", "ask", {"path": "a.mp3"}))
    assert ans.decision == "uncertain" and "timed out" in caplog.text


def test_disambiguate_writes_to_out_csv(tmp_path: Path) -> None:
    # policy-apply pair (accent add) so the confirmed row flips, isolating the out-csv behavior
    review = _review(
        tmp_path,
        ["skip", "resolve_artist", "REVIEW", "x.mp3", "artist", "Beyonce", "Beyoncé", "ev"],
    )
    out = str(tmp_path / "out.csv")
    ans = ResearchAnswer("confirm", "Beyoncé", 0.9, ("https://mb.org/b",), "ok")
    pipeline.disambiguate(review, Providers(researcher=_FixedResearcher(ans)), out_csv=out)
    assert pipeline.read_review(out)[0].apply == "apply"
    assert pipeline.read_review(review)[0].apply == "skip"  # original untouched
