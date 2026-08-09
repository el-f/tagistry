"""Layer-1 hardened checks: the score-alone trap must never pass."""

from __future__ import annotations

from typing import cast

from fakes import FakeMusicBrainz
from tagistry.checks import Checks
from tagistry.providers.musicbrainz import MusicBrainz


def _checks(fake: FakeMusicBrainz) -> Checks:
    return Checks(cast(MusicBrainz, fake))


def test_is_real_artist_requires_name_match_not_just_score() -> None:
    # the Lilo & Stitch trap: MB returns a 100 score for a fuzzy non-match
    c = _checks(FakeMusicBrainz(artists={"Lilo & Stitch": ("Lilo Lilo", 100), "Radiohead": ("Radiohead", 100)}))
    assert c.is_real_artist("Radiohead")
    assert not c.is_real_artist("Lilo & Stitch")  # score 100 but name mismatch -> not real


def test_is_real_artist_accepts_fuller_official_name() -> None:
    c = _checks(FakeMusicBrainz(artists={"Hall & Oates": ("Daryl Hall & John Oates", 100)}))
    assert c.is_real_artist("Hall & Oates")  # subset of the fuller name


def test_is_real_artist_accepts_okina_canonical_spelling() -> None:
    # MB spells it with the okina (Lm), the tag with an ASCII quote -- a 100 score on the same act.
    c = _checks(FakeMusicBrainz(artists={"Israel Kamakawiwo'ole": ("Israel Kamakawiwoʻole", 100)}))
    assert c.is_real_artist("Israel Kamakawiwo'ole")


def test_is_real_artist_accepts_canonical_contained_in_the_tag() -> None:
    # The tag carries the ensemble AND the conductor; MB returns just the conductor.
    c = _checks(FakeMusicBrainz(artists={"Philadelphia Orchestra with Eugene Ormandy": ("Eugene Ormandy", 100)}))
    assert c.is_real_artist("Philadelphia Orchestra with Eugene Ormandy")


def test_artist_for_title_rejects_common_title_hidden_by_a_narrow_window() -> None:
    # Real MB data: the top 2 filter out on their suffix, so a 3-wide window leaves ONE survivor.
    c = _checks(
        FakeMusicBrainz(
            tops_multi={
                "Over the Rainbow": [
                    ("Over the Rainbow (Somewhere Over the Rainbow)", "Marusha", 100),
                    ("Over the Rainbow (Somewhere Over the Rainbow)", "Marusha", 100),
                    ("Over the Rainbow", "Glenn Miller & His Orchestra", 98),
                    ("Over the Rainbow", "Judy Garland", 100),
                    ("Over the Rainbow", "Papa John Creach", 100),
                ]
            }
        )
    )
    assert c.artist_for_title("Over the Rainbow") is None


def test_artist_for_title_returns_canonical_on_strong_match() -> None:
    c = _checks(FakeMusicBrainz(tops={"He Mele No Lilo": ("He Mele No Lilo", "Mark Keali'i Ho'omalu", 100)}))
    assert c.artist_for_title("He Mele No Lilo") == "Mark Keali'i Ho'omalu"


def test_artist_for_title_rejects_weak_or_mismatched() -> None:
    c = _checks(FakeMusicBrainz(tops={"Song": ("A Different Song", "X", 100), "Weak": ("Weak", "Y", 80)}))
    assert c.artist_for_title("Song") is None  # title mismatch
    assert c.artist_for_title("Weak") is None  # score below floor


def test_artist_for_title_rejects_ambiguous_common_title() -> None:
    # "Eye In The Sky" -> many different artists -> don't guess (the Murray Gold misfire)
    c = _checks(
        FakeMusicBrainz(
            tops_multi={
                "Eye In The Sky": [
                    ("Eye In The Sky", "The Alan Parsons Project", 100),
                    ("Eye In The Sky", "Murray Gold", 100),
                ]
            }
        )
    )
    assert c.artist_for_title("Eye In The Sky") is None  # matches disagree -> uncertain


def test_is_collaboration_both_real() -> None:
    c = _checks(
        FakeMusicBrainz(
            artists={"The Weeknd": ("The Weeknd", 100), "Ariana Grande": ("Ariana Grande", 100), "Red Band": ("", 0)}
        )
    )
    assert c.is_collaboration("The Weeknd & Ariana Grande")
    assert not c.is_collaboration("The Weeknd & Red Band")  # right side not a real artist


def test_is_collaboration_all_co_lead_separators() -> None:
    c = _checks(
        FakeMusicBrainz(
            artists={
                "Vanic": ("Vanic", 100),
                "K.Flay": ("K.Flay", 100),
                "benny blanco": ("benny blanco", 100),
                "Halsey": ("Halsey", 100),
                "Khalid": ("Khalid", 100),
                "Nicky Romero": ("Nicky Romero", 100),
                "Krewella": ("Krewella", 100),
                "Gloria Estefan": ("Gloria Estefan", 100),
                "Miami Sound Machine": ("Miami Sound Machine", 100),
                "Tiesto": ("Tiesto", 100),
            }
        )
    )
    assert c.is_collaboration("Vanic x K.Flay")  # 'x' join
    assert c.is_collaboration("Nicky Romero vs Krewella")  # 'vs' join
    assert c.is_collaboration("benny blanco, Halsey & Khalid")  # comma + '&', three-way
    assert c.is_collaboration("Gloria Estefan / Miami Sound Machine")  # '/' join
    assert c.is_collaboration("Vanic × Tiesto")  # unicode multiplication-sign join


def test_is_collaboration_excludes_feat_and_single_names() -> None:
    c = _checks(
        FakeMusicBrainz(
            artists={
                "Timbaland": ("Timbaland", 100),
                "One Republic": ("One Republic", 100),
                "Stavros Grekis": ("Stavros Grekis", 100),
                "His Original Bouzouki Orchestra": ("", 0),  # backing band, not a registered artist
            }
        )
    )
    # 'pres.'/'feat' is a guest feature, not a co-lead join -> not a collaboration
    assert not c.is_collaboration("Timbaland Pres. One Republic")
    # a backing band fails the all-parts-real gate -> left to split
    assert not c.is_collaboration("Stavros Grekis & His Original Bouzouki Orchestra")
