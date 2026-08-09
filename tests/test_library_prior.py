"""LibraryPrior offline self-consistency."""

from __future__ import annotations

from conftest import make_track
from tagistry.providers import LibraryPrior


def test_known_needs_two_occurrences() -> None:
    lp = LibraryPrior.from_tracks(
        [make_track(artist="Metallica"), make_track(artist="Metallica"), make_track(artist="OneOff")]
    )
    assert lp.is_known_artist("Metallica")
    assert not lp.is_known_artist("OneOff")


def test_known_is_case_and_amp_insensitive() -> None:
    lp = LibraryPrior.from_tracks([make_track(artist="AC/DC"), make_track(artist="ac/dc")])
    assert lp.is_known_artist("AC/DC")


def test_add_verified_bypasses_count() -> None:
    lp = LibraryPrior()
    lp.add_verified("Radiohead")
    lp.finalize()
    assert lp.is_known_artist("Radiohead")


def test_title_is_known_artist() -> None:
    lp = LibraryPrior.from_tracks([make_track(artist="Shinedown"), make_track(artist="Shinedown")])
    assert lp.title_is_known_artist(make_track(artist="45", title="Shinedown"))
    assert not lp.title_is_known_artist(make_track(artist="45", title="Some Song"))


def test_blank_artist_ignored() -> None:
    lp = LibraryPrior.from_tracks([make_track(artist=""), make_track(artist="  ")])
    assert not lp.is_known_artist("")


def _prior_with(*artists: str) -> LibraryPrior:
    # each name twice so it clears the known threshold
    return LibraryPrior.from_tracks([make_track(artist=a) for a in artists for _ in range(2)])


def test_single_act_keeps_colead_when_both_sides_known() -> None:
    lp = _prior_with("The Weeknd", "Ariana Grande")
    assert lp.is_single_act("The Weeknd & Ariana Grande")
    assert lp.is_single_act("The Weeknd, Ariana Grande")  # comma co-lead too


def test_single_act_splits_when_a_side_is_unknown() -> None:
    lp = _prior_with("The Weeknd")
    assert not lp.is_single_act("The Weeknd & Nobody Knows Them")


def test_single_act_rejects_feat_guest() -> None:
    lp = _prior_with("Drake", "Rihanna")  # both known, but feat = guest, not co-lead
    assert not lp.is_single_act("Drake feat. Rihanna")


def test_single_act_backing_band_pattern_without_prior() -> None:
    lp = LibraryPrior()
    lp.finalize()
    assert lp.is_single_act("CODY & The Salvation Army")  # is_probably_band, no library needed


def test_single_act_curated_famous_band() -> None:
    lp = LibraryPrior()
    lp.finalize()
    assert lp.is_single_act("Earth, Wind & Fire")


def test_single_act_blank_is_false() -> None:
    lp = LibraryPrior()
    lp.finalize()
    assert not lp.is_single_act("   ")
