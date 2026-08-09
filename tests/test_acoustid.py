"""identify() must reject an ambiguous fingerprint tie instead of coin-flipping a sibling.

An AcoustID lookup can return several DIFFERENT songs from one album at the same top score
(album-level fingerprint collision). The old code kept whichever the API listed first, which
silently retitled a correct tag to a random album track. identify() now returns the match only
when the near-top candidates all point to ONE song; a real tie between distinct songs -> None.
"""

from __future__ import annotations

import socket

import pytest

from tagistry.providers.acoustid import AcoustID


def _patch(monkeypatch: pytest.MonkeyPatch, candidates: list[tuple[float, str, str, str]]) -> None:
    """candidates: (score, rid, title, artist), in the (arbitrary) order the API returns them."""
    monkeypatch.setattr("tagistry.providers.acoustid.acoustid.match", lambda key, path: iter(candidates))


def _ac() -> AcoustID:
    return AcoustID(api_key="test-key")


def test_single_song_dominant_returns_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [(0.99, "r1", "Clocks", "Coldplay")])
    m = _ac().identify("x.mp3")
    assert m is not None and m.title == "Clocks"


def test_distinct_songs_tie_is_ambiguous_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two different Eminem songs at the same top score -> cannot tell which -> None.
    _patch(
        monkeypatch,
        [
            (1.00, "r1", "Kings Never Die", "Eminem"),
            (1.00, "r2", "No Love", "Eminem"),
        ],
    )
    assert _ac().identify("x.mp3") is None


def test_marker_variant_tie_is_ambiguous_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same score for plain vs (instrumental): cannot tell which the file is -> None, keep the clean tag.
    _patch(
        monkeypatch,
        [
            (0.99, "r1", "No Love (instrumental)", "Eminem"),
            (0.99, "r2", "No Love", "Eminem"),
        ],
    )
    assert _ac().identify("x.mp3") is None


def test_same_recording_listed_twice_is_not_a_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    # AcoustID lists one recording under several releases; same title (any case) -> not a tie.
    _patch(
        monkeypatch,
        [
            (0.99, "r1", "Clocks", "Coldplay"),
            (0.99, "r2", "clocks", "Coldplay"),
        ],
    )
    m = _ac().identify("x.mp3")
    assert m is not None and m.title == "Clocks"


def test_lower_scoring_distinct_song_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # A distinct song well below the top score is noise, not a tie -> top still wins.
    _patch(
        monkeypatch,
        [
            (0.98, "r1", "Clocks", "Coldplay"),
            (0.80, "r2", "Yellow", "Coldplay"),
        ],
    )
    m = _ac().identify("x.mp3")
    assert m is not None and m.title == "Clocks"


def test_tie_below_min_score_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [(0.50, "r1", "Clocks", "Coldplay")])
    assert _ac().identify("x.mp3") is None


def test_lookup_bounds_the_socket_timeout_and_restores_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # pyacoustid's urllib has no per-call timeout; identify() must bound it and restore the default after.
    seen: dict[str, float | None] = {}

    def capturing_match(key: str, path: str) -> object:
        seen["during"] = socket.getdefaulttimeout()
        return iter([(0.99, "r1", "Clocks", "Coldplay")])

    monkeypatch.setattr("tagistry.providers.acoustid.acoustid.match", capturing_match)
    before = socket.getdefaulttimeout()
    _ac().identify("x.mp3")
    assert seen["during"] == 20.0  # bounded during the network call
    assert socket.getdefaulttimeout() == before  # restored after


def test_identify_is_memoized_per_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three fixers identify() the same file in one scan; fpcalc + lookup must run once per path.
    calls = {"n": 0}

    def counting_match(key: str, path: str) -> object:
        calls["n"] += 1
        return iter([(0.99, "r1", "Clocks", "Coldplay")])

    monkeypatch.setattr("tagistry.providers.acoustid.acoustid.match", counting_match)
    ac = _ac()
    first, second = ac.identify("x.mp3"), ac.identify("x.mp3")
    assert first is second and calls["n"] == 1  # second call served from the per-path cache
    ac.identify("y.mp3")
    assert calls["n"] == 2  # a different path fingerprints again
