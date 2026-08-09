"""Golden cases for the deterministic adjudication policies."""

from __future__ import annotations

import pytest

from tagistry.policies import adjudicate_change


@pytest.mark.parametrize(
    ("current", "proposed", "verdict"),
    [
        # keep-accents policy: adding a proper accent is canonical -> apply; stripping one -> reject
        ("Beyonce", "Beyoncé", "apply"),
        ("Antonio Carlos Jobim", "Antônio Carlos Jobim", "apply"),
        ("Beyoncé", "Beyonce", "reject"),
        ("Malagueña", "Malaguena", "reject"),
        # pure case/spacing fold (same match key, no accent change) -> apply
        ("the beatles", "The Beatles", "apply"),
        # verified co-lead added on the end -> apply
        ("Jay-Z", "Jay-Z & Kanye West", "apply"),
        ("Calvin Harris", "Calvin Harris feat. Rihanna", "apply"),
        # MB-canonical drops user context (a shorter subset of the current) -> flag
        ("Song (Live at Wembley)", "Song", "flag"),
        ("Yesterday - Anniversary Mix", "Yesterday", "flag"),
        # unverifiable version marker introduced -> flag
        ("Old Town Road", "Old Town Road (Remix)", "flag"),
        ("Song", "Song (Acoustic Version)", "flag"),
        # an unrelated rewrite is not auto-trusted -> flag
        ("Some Title", "A Totally Different Title", "flag"),
    ],
)
def test_adjudicate_change_verdicts(current: str, proposed: str, verdict: str) -> None:
    assert adjudicate_change(current, proposed)[0] == verdict


def test_adjudicate_change_returns_a_reason() -> None:
    verdict, reason = adjudicate_change("Beyonce", "Beyoncé")
    assert verdict == "apply" and "accent" in reason.lower()


def test_adjudicate_rejects_clearing_a_field() -> None:
    # an empty proposed must never auto-apply (it would delete the value)
    assert adjudicate_change("John Doe", "")[0] == "reject"
    assert adjudicate_change("John Doe", "   ")[0] == "reject"


@pytest.mark.parametrize("proposed", ["Michael and", "Michael feat.", "Michael ft.", "Michael x"])
def test_adjudicate_dangling_coartist_keyword_is_not_applied(proposed: str) -> None:
    # a trailing co-artist keyword with NO name after it is corrupt, never 'apply'
    assert adjudicate_change("Michael", proposed)[0] == "flag"


def test_adjudicate_reorder_is_not_called_context_drop() -> None:
    # 'John Smith' -> 'Smith John' is a reorder, not a context loss -> not the drops-context reason
    _verdict, reason = adjudicate_change("John Smith", "Smith John")
    assert "context" not in reason.lower()
