"""Golden tests for the six fixers, reproducing the session's proven results.

Providers are faked (hermetic). Cases are drawn from the real review CSVs.
"""

from __future__ import annotations

from typing import cast

from conftest import make_track
from fakes import FakeAcoustID, FakeDiscogs, FakeMusicBrainz, FakeResearcher
from tagistry.domain import Confidence
from tagistry.fixers import (
    album_junk,
    ascii_dash,
    blank_id,
    canonicalize,
    feat_to_title,
    flip,
    genre_fill,
    merged_field,
    multi_artist,
    normalize,
    resolve_artist,
    title_junk,
    year_fill,
)
from tagistry.providers import Discogs, LibraryPrior, Providers, Researcher
from tagistry.providers.acoustid import AcoustID, AcoustIDMatch
from tagistry.providers.musicbrainz import MusicBrainz
from tagistry.research import ResearchAnswer


def _mb(fake: FakeMusicBrainz) -> MusicBrainz:
    return cast(MusicBrainz, fake)


# --- multi_artist -----------------------------------------------------------


def test_multi_artist_keep_real_band() -> None:
    mb = _mb(FakeMusicBrainz(artists={"Angus & Julia Stone": ("Angus & Julia Stone", 100)}))
    t = make_track(artist="Angus & Julia Stone")
    assert multi_artist(t, Providers(musicbrainz=mb)) == []


def test_multi_artist_keep_fuller_official_name() -> None:
    # subset: query tokens all inside MB's fuller name -> KEEP
    mb = _mb(FakeMusicBrainz(artists={"Bruce Hornsby, The Range": ("Bruce Hornsby & the Range", 100)}))
    t = make_track(artist="Bruce Hornsby, The Range")
    assert multi_artist(t, Providers(musicbrainz=mb)) == []


def test_multi_artist_split_is_review_not_auto_apply() -> None:
    # A split drops the other names, so even a confirmed primary stays REVIEW and is always staged.
    mb = _mb(FakeMusicBrainz(artists={"2Pac, Outlawz": ("", 0), "2Pac": ("2Pac", 100)}))
    t = make_track(artist="2Pac, Outlawz")
    (p,) = multi_artist(t, Providers(musicbrainz=mb))
    assert (p.field, p.current, p.proposed, p.confidence) == ("artist", "2Pac, Outlawz", "2Pac", Confidence.REVIEW)
    assert p.evidence.reason == 'MB primary "2Pac" 100'


def test_multi_artist_split_feat() -> None:
    mb = _mb(FakeMusicBrainz(artists={"2WEI feat. Edda Hayes": ("", 0), "2WEI": ("2WEI", 100)}))
    t = make_track(artist="2WEI feat. Edda Hayes")
    (p,) = multi_artist(t, Providers(musicbrainz=mb))
    # feat_to_title has priority and moves the guest at HIGH; multi_artist's split stays REVIEW-capped.
    assert p.proposed == "2WEI" and p.confidence is Confidence.REVIEW


def test_multi_artist_backing_band_review() -> None:
    mb = _mb(FakeMusicBrainz(artists={"CODY & The Salvation Army": ("The Salvation Army", 100), "CODY": ("", 0)}))
    t = make_track(artist="CODY & The Salvation Army")
    (p,) = multi_artist(t, Providers(musicbrainz=mb))
    assert (p.proposed, p.confidence) == ("CODY", Confidence.REVIEW)
    assert "backing-band" in p.evidence.reason


def test_multi_artist_split_low_when_primary_unconfirmed() -> None:
    # MB knows the primary but weakly (score < 90) -> LOW, still proposes the split
    mb = _mb(FakeMusicBrainz(artists={"A, B": ("", 0), "A": ("A Band", 60)}))
    (p,) = multi_artist(make_track(artist="A, B"), Providers(musicbrainz=mb))
    assert (p.proposed, p.confidence) == ("A", Confidence.LOW)


def test_multi_artist_offline_emits_review() -> None:
    t = make_track(artist="2Pac, Outlawz")
    (p,) = multi_artist(t, Providers())  # no MB
    assert (p.proposed, p.confidence) == ("2Pac", Confidence.REVIEW)


def test_multi_artist_offline_keeps_colead_when_both_sides_known() -> None:
    from tagistry.providers import LibraryPrior

    lp = LibraryPrior.from_tracks([make_track(artist=a) for a in ("Angus", "Angus", "Julia", "Julia")])
    # offline, no MB: both sides are known library artists -> co-lead, keep whole
    assert multi_artist(make_track(artist="Angus & Julia"), Providers(library=lp)) == []


def test_multi_artist_single_artist_noop() -> None:
    assert multi_artist(make_track(artist="Radiohead"), Providers()) == []


def test_multi_artist_keeps_amp_collaboration() -> None:
    # "A & B" with both real artists = co-lead collab -> keep joint, don't drop the co-artist
    mb = _mb(
        FakeMusicBrainz(
            artists={
                "The Weeknd & Ariana Grande": ("The Weeknd", 95),
                "The Weeknd": ("The Weeknd", 100),
                "Ariana Grande": ("Ariana Grande", 100),
            }
        )
    )
    assert multi_artist(make_track(artist="The Weeknd & Ariana Grande"), Providers(musicbrainz=mb)) == []


def test_multi_artist_still_splits_comma_primary_plus_backing() -> None:
    # comma is NOT a collaboration signal here -> split to primary stays correct
    mb = _mb(FakeMusicBrainz(artists={"2Pac, Outlawz": ("", 0), "2Pac": ("2Pac", 100)}))
    (p,) = multi_artist(make_track(artist="2Pac, Outlawz"), Providers(musicbrainz=mb))
    assert p.proposed == "2Pac"


# --- feat_to_title ----------------------------------------------------------


def test_feat_to_title_moves_feat() -> None:
    t = make_track(artist="The Weeknd feat. Ariana Grande", title="Save Your Tears")
    props = feat_to_title(t, Providers())
    assert {p.field: p.proposed for p in props} == {
        "artist": "The Weeknd",
        "title": "Save Your Tears (feat. Ariana Grande)",
    }
    assert all(p.confidence is Confidence.HIGH for p in props)


def test_feat_to_title_skips_title_if_already_has_feat() -> None:
    t = make_track(artist="A ft. B", title="Song (feat. B)")
    (p,) = feat_to_title(t, Providers())  # only the artist row; title already marked
    assert p.field == "artist" and p.proposed == "A"


def test_feat_to_title_no_feat_in_artist() -> None:
    assert feat_to_title(make_track(artist="A, B", title="Song"), Providers()) == []


def test_feat_to_title_keeps_a_different_guest_from_the_title() -> None:
    # title already credits Drake; stripping the artist's feat would DROP Future -> leave it alone
    t = make_track(artist="Drake feat. Future", title="Life Is Good (feat. Drake)")
    assert feat_to_title(t, Providers()) == []


def test_feat_to_title_strips_paren_wrapped_feat() -> None:
    t = make_track(artist="A (feat. B)", title="Song")
    props = feat_to_title(t, Providers())
    assert {p.field: p.proposed for p in props} == {"artist": "A", "title": "Song (feat. B)"}


def test_feat_to_title_high_when_mb_confirms_the_primary() -> None:
    mb = _mb(FakeMusicBrainz(recordings={("Save Your Tears", "The Weeknd"): 100}))
    t = make_track(artist="The Weeknd feat. Ariana Grande", title="Save Your Tears")
    props = feat_to_title(t, Providers(musicbrainz=mb))
    assert props and all(p.confidence is Confidence.HIGH for p in props)


def test_feat_to_title_review_when_mb_does_not_confirm() -> None:
    # MB can't confirm the primary recorded this song -> the regex rearrange stays REVIEW, not HIGH
    mb = _mb(FakeMusicBrainz(recordings={}))
    t = make_track(artist="The Weeknd feat. Ariana Grande", title="Save Your Tears")
    props = feat_to_title(t, Providers(musicbrainz=mb))
    assert props and all(p.confidence is Confidence.REVIEW for p in props)


# --- canonicalize -----------------------------------------------------------


def test_canonicalize_uses_mb_recording() -> None:
    m = AcoustIDMatch(0.97, "JAY-Z", "Otis", "rid-jz")
    mb = _mb(FakeMusicBrainz(by_id={"rid-jz": ("Otis", "JAY-Z & Kanye West")}))
    t = make_track(path="x.opus", artist="Jay-Z", title="Otis")
    props = canonicalize(t, Providers(acoustid=_ac(m), musicbrainz=mb))
    assert {p.field: p.proposed for p in props} == {"artist": "JAY-Z & Kanye West"}
    assert all(p.confidence is Confidence.REVIEW for p in props)  # overwrites, never auto


def test_canonicalize_marks_special_version_in_title() -> None:
    # tags say the plain title; the audio is the remix -> MB title carries the marker
    m = AcoustIDMatch(0.96, "The Weeknd", "Save Your Tears", "rid-syt")
    mb = _mb(FakeMusicBrainz(by_id={"rid-syt": ("Save Your Tears (Remix) (feat. Ariana Grande)", "The Weeknd")}))
    t = make_track(path="x.opus", artist="The Weeknd", title="Save Your Tears")
    (p,) = canonicalize(t, Providers(acoustid=_ac(m), musicbrainz=mb))
    assert p.field == "title" and p.proposed == "Save Your Tears (Remix) (feat. Ariana Grande)"


def test_canonicalize_drops_codec_marker_no_proposal() -> None:
    # MB gives a 5.1/surround title; the codec marker is stripped -> no change
    m = AcoustIDMatch(0.97, "Steven Wilson", "Detonation", "rid-det")
    mb = _mb(FakeMusicBrainz(by_id={"rid-det": ("Detonation (5.1 mix)", "Steven Wilson")}))
    t = make_track(path="x.opus", artist="Steven Wilson", title="Detonation")
    assert canonicalize(t, Providers(acoustid=_ac(m), musicbrainz=mb)) == []


def test_canonicalize_moves_mb_feat_to_title() -> None:
    # MB stores feat in the artist-credit; canonicalize moves it to the title (convention)
    m = AcoustIDMatch(0.96, "x", "x", "rid-b")
    mb = _mb(FakeMusicBrainz(by_id={"rid-b": ("Budget", "Megan Thee Stallion feat. Latto")}))
    t = make_track(path="x.opus", artist="Megan Thee Stallion", title="Budget")
    (p,) = canonicalize(t, Providers(acoustid=_ac(m), musicbrainz=mb))
    assert p.field == "title" and p.proposed == "Budget (feat. Latto)"  # artist already correct


def test_canonicalize_folds_curly_quotes_no_noise() -> None:
    # MB gives a curly apostrophe; the tag has a straight one -> folded -> no proposal
    m = AcoustIDMatch(0.96, "x", "x", "rid-q")
    mb = _mb(FakeMusicBrainz(by_id={"rid-q": ("Don’t Go Outside", "Artist")}))
    t = make_track(path="x.opus", artist="Artist", title="Don't Go Outside")
    assert canonicalize(t, Providers(acoustid=_ac(m), musicbrainz=mb)) == []


def test_canonicalize_falls_back_to_acoustid_metadata_without_mb() -> None:
    m = AcoustIDMatch(0.95, "Daft Punk", "Da Funk", "rid-df")
    t = make_track(path="x.mp3", artist="daft punk", title="Wrong Title")
    (p,) = canonicalize(t, Providers(acoustid=_ac(m)))  # no MB -> use AcoustID's own metadata
    assert p.field == "title" and p.proposed == "Da Funk"


def test_canonicalize_needs_acoustid() -> None:
    assert canonicalize(make_track(path="x.mp3", artist="A", title="B"), Providers()) == []


def test_canonicalize_no_match_untouched() -> None:
    assert canonicalize(make_track(path="x.mp3"), Providers(acoustid=_ac(None))) == []


# --- resolve_artist ---------------------------------------------------------


def test_resolve_artist_recovers_from_title() -> None:
    # "Lilo & Stitch" isn't a real artist; the title resolves to the real one via MB
    mb = _mb(
        FakeMusicBrainz(
            artists={"Lilo & Stitch": ("Lilo Lilo", 100), "Lilo": ("", 0), "Stitch": ("", 0)},
            tops={"He Mele No Lilo": ("He Mele No Lilo", "Mark Keali'i Ho'omalu", 100)},
        )
    )
    t = make_track(artist="Lilo & Stitch", title="He Mele No Lilo")
    (p,) = resolve_artist(t, Providers(musicbrainz=mb))
    assert p.field == "artist" and p.proposed == "Mark Keali'i Ho'omalu" and p.confidence is Confidence.REVIEW


def test_resolve_artist_leaves_real_artist() -> None:
    mb = _mb(FakeMusicBrainz(artists={"Radiohead": ("Radiohead", 100)}))
    assert resolve_artist(make_track(artist="Radiohead", title="Creep"), Providers(musicbrainz=mb)) == []


def test_resolve_artist_uses_researcher_for_residue() -> None:
    # deterministic checks can't settle it -> the pluggable researcher does, with a citation
    mb = _mb(FakeMusicBrainz(artists={"???": ("", 0)}))
    answer = ResearchAnswer(decision="artist", value="Real Artist", confidence=0.95, sources=("https://mb.org/x",))
    r = cast(Researcher, FakeResearcher(answer))
    t = make_track(artist="???", title="Obscure Track")
    (p,) = resolve_artist(t, Providers(musicbrainz=mb, researcher=r))
    assert p.proposed == "Real Artist" and p.confidence is Confidence.REVIEW


def test_resolve_artist_ignores_uncertain_researcher() -> None:
    mb = _mb(FakeMusicBrainz(artists={"???": ("", 0)}))
    uncertain = ResearchAnswer(decision="uncertain")
    r = cast(Researcher, FakeResearcher(uncertain))
    assert resolve_artist(make_track(artist="???", title="Obscure"), Providers(musicbrainz=mb, researcher=r)) == []


# --- flip -------------------------------------------------------------------


def _prior(*known: str) -> LibraryPrior:
    lp = LibraryPrior()
    for name in known:
        lp.add_artist(name)
        lp.add_artist(name)  # twice -> known
    lp.finalize()
    return lp


def test_flip_high_confirmed_swap() -> None:
    # HIGH needs a name-matched recording: a recording titled '45' credited to 'Shinedown'
    mb = _mb(
        FakeMusicBrainz(
            recordings={("45", "Shinedown"): 100, ("Shinedown", "45"): 0},
            tops={"45": ("45", "Shinedown", 100)},
        )
    )
    lp = _prior("Shinedown")
    t = make_track(artist="45", title="Shinedown")
    props = flip(t, Providers(library=lp, musicbrainz=mb))
    assert len(props) == 2  # both sides of the swap, always together
    assert {p.field: p.proposed for p in props} == {"artist": "Shinedown", "title": "45"}
    assert {p.field: p.current for p in props} == {"artist": "45", "title": "Shinedown"}
    assert all(p.confidence is Confidence.HIGH for p in props)


def test_flip_high_score_but_no_name_match_makes_no_proposal() -> None:
    # High swapped score but no name-match (fuzzy hit): the name-match gates emission, so flip is silent.
    mb = _mb(
        FakeMusicBrainz(
            recordings={("45", "Shinedown"): 100, ("Shinedown", "45"): 0},
            tops={"45": ("Forty Five", "Some Other Band", 100)},  # fuzzy: title/artist don't match
        )
    )
    t = make_track(artist="45", title="Shinedown")
    assert flip(t, Providers(library=_prior("Shinedown"), musicbrainz=mb)) == []


def test_flip_rejects_when_current_orientation_real() -> None:
    # soundtrack trap: neither orientation strongly beats the other
    mb = _mb(FakeMusicBrainz(recordings={("Freedom", "Django Unchained"): 50, ("Django Unchained", "Freedom"): 0}))
    lp = _prior("Django Unchained")
    t = make_track(artist="Freedom", title="Django Unchained")
    assert flip(t, Providers(library=lp, musicbrainz=mb)) == []


def test_flip_review_when_current_also_plausible() -> None:
    # Name-matched swap, but the current orientation is also plausible (score 80) -> REVIEW, not HIGH.
    mb = _mb(FakeMusicBrainz(recordings={("A", "B"): 95, ("B", "A"): 80}, tops={"A": ("A", "B", 95)}))
    lp = _prior("B")
    t = make_track(artist="A", title="B")
    props = flip(t, Providers(library=lp, musicbrainz=mb))
    assert len(props) == 2 and all(p.confidence is Confidence.REVIEW for p in props)
    assert {p.field: p.proposed for p in props} == {"artist": "B", "title": "A"}


def test_flip_offline_makes_no_proposal() -> None:
    # Without MusicBrainz a flip is an unverified guess, so offline flip proposes nothing.
    lp = _prior("Shinedown")
    assert flip(make_track(artist="45", title="Shinedown"), Providers(library=lp)) == []


def test_flip_needs_prior() -> None:
    assert flip(make_track(artist="45", title="Shinedown"), Providers()) == []


def test_flip_skips_when_title_not_known_artist() -> None:
    lp = _prior("Shinedown")
    t = make_track(artist="Shinedown", title="Sound of Madness")
    assert flip(t, Providers(library=lp)) == []


# --- merged_field -----------------------------------------------------------


def test_merged_field_strip_confirmed_prefix() -> None:
    t = make_track(artist="Miley Cyrus", title='Miley Cyrus - The Backyard Sessions - "Jolene"')
    (p,) = merged_field(t, Providers())
    assert p.proposed == 'The Backyard Sessions - "Jolene"' and p.confidence is Confidence.HIGH


def test_merged_field_no_dash() -> None:
    assert merged_field(make_track(artist="Radiohead", title="Karma Police"), Providers()) == []


def test_merged_field_prefix_mismatch() -> None:
    t = make_track(artist="Radiohead", title="Metallica - One")
    assert merged_field(t, Providers()) == []


# --- title_junk -------------------------------------------------------------


def test_title_junk_remaster() -> None:
    (p,) = title_junk(make_track(title="Karma Police (Remastered)"), Providers())
    assert p.proposed == "Karma Police"


def test_title_junk_official_video() -> None:
    (p,) = title_junk(make_track(title="Bad Guy (Official Video)"), Providers())
    assert p.proposed == "Bad Guy"


def test_title_junk_tracknum_prefix() -> None:
    (p,) = title_junk(make_track(title="01. For Whom The Bell Tolls"), Providers())
    assert p.proposed == "For Whom The Bell Tolls"


def test_title_junk_keeps_real_parenthetical() -> None:
    assert title_junk(make_track(title="Sweet Dreams (Are Made of This)"), Providers()) == []


def test_title_junk_year_remaster_tail() -> None:
    (p,) = title_junk(make_track(title="Fade To Black - 2016 Remastered"), Providers())
    assert p.proposed == "Fade To Black"


def test_title_junk_skips_when_only_remaster_metadata_remains() -> None:
    # stripping 'Remaster' leaves '2011 Digital' -- only metadata, not a real title -> don't apply
    assert title_junk(make_track(title="2011 Digital Remaster"), Providers()) == []


def test_title_junk_keeps_bare_remaster_noun_title() -> None:
    # A trailing bare 'Remaster' with no dash/year/'-ed' is likely a real title word -- stripping mangles it.
    assert title_junk(make_track(title="The Remaster"), Providers()) == []
    assert title_junk(make_track(title="Original Remaster"), Providers()) == []


# --- ascii_dash -------------------------------------------------------------


def test_ascii_dash_en_dash_title() -> None:
    (p,) = ascii_dash(make_track(title="Song – Live"), Providers())
    assert (p.field, p.proposed, p.confidence) == ("title", "Song - Live", Confidence.HIGH)


def test_ascii_dash_both_fields() -> None:
    props = ascii_dash(make_track(artist="A — B", title="C – D"), Providers())
    assert {p.field: p.proposed for p in props} == {"artist": "A - B", "title": "C - D"}


def test_ascii_dash_ascii_untouched() -> None:
    assert ascii_dash(make_track(artist="Jay-Z", title="99 Problems"), Providers()) == []


# --- normalize --------------------------------------------------------------


def test_normalize_folds_fullwidth_asterisks() -> None:
    # a censored word tagged with fullwidth ＊ (from the filename) -> ASCII ** matches the canonical
    (p,) = normalize(make_track(title="Da＊＊it (Remix)"), Providers())
    assert p.proposed == "Da**it (Remix)" and p.confidence is Confidence.HIGH


def test_normalize_tightens_spaced_censor() -> None:
    (p,) = normalize(make_track(title="Da * * it Now"), Providers())
    assert p.proposed == "Da**it Now"


def test_normalize_folds_other_fullwidth_punct() -> None:
    (p,) = normalize(make_track(title="Song ＂Jolene＂"), Providers())  # fullwidth quotes
    assert p.proposed == 'Song "Jolene"'


def test_normalize_collapses_runs() -> None:
    (p,) = normalize(make_track(title="Hello   World"), Providers())
    assert p.proposed == "Hello World"


def test_normalize_leaves_single_asterisk_math() -> None:
    assert normalize(make_track(title="5 * 3 Theme"), Providers()) == []


def test_normalize_clean_untouched() -> None:
    assert normalize(make_track(artist="Jay-Z", title="99 Problems"), Providers()) == []


# --- blank_id ---------------------------------------------------------------


def _ac(match: AcoustIDMatch | None) -> AcoustID:
    return cast(AcoustID, FakeAcoustID(match))


def test_blank_id_fills_both() -> None:
    m = AcoustIDMatch(0.95, "Daft Punk", "Around the World", "rid-1")
    t = make_track(path="blank.mp3")
    props = blank_id(t, Providers(acoustid=_ac(m)))
    assert {p.field: p.proposed for p in props} == {"artist": "Daft Punk", "title": "Around the World"}
    assert all(p.confidence is Confidence.HIGH for p in props)


def test_blank_id_only_missing_field() -> None:
    m = AcoustIDMatch(0.88, "Daft Punk", "One More Time", "rid-2")
    t = make_track(artist="Daft Punk")  # title blank only
    (p,) = blank_id(t, Providers(acoustid=_ac(m)))
    assert p.field == "title" and p.confidence is Confidence.REVIEW  # 0.88 < 0.92


def test_blank_id_no_match_untouched() -> None:
    assert blank_id(make_track(path="b.mp3"), Providers(acoustid=_ac(None))) == []


def test_blank_id_high_when_acoustid_and_mb_agree() -> None:
    m = AcoustIDMatch(0.95, "Daft Punk", "Around the World", "rid-1")
    mb = _mb(FakeMusicBrainz(by_id={"rid-1": ("Around the World", "Daft Punk")}))
    props = blank_id(make_track(path="blank.mp3"), Providers(acoustid=_ac(m), musicbrainz=mb))
    assert all(p.confidence is Confidence.HIGH for p in props)  # both fingerprint signals concur


def test_blank_id_review_when_acoustid_and_mb_disagree() -> None:
    # AcoustID raw metadata disagrees with the MB canonical: propose MB, but at REVIEW.
    m = AcoustIDMatch(0.99, "Wrong Artist", "Wrong Title", "rid-1")
    mb = _mb(FakeMusicBrainz(by_id={"rid-1": ("Around the World", "Daft Punk")}))
    props = blank_id(make_track(path="blank.mp3"), Providers(acoustid=_ac(m), musicbrainz=mb))
    assert {p.field: p.proposed for p in props} == {"artist": "Daft Punk", "title": "Around the World"}
    assert all(p.confidence is Confidence.REVIEW for p in props)


def test_blank_id_0_92_boundary_is_the_high_cutoff() -> None:
    # 0.92 is the auto-apply floor: 0.92 -> HIGH, 0.9199 -> REVIEW (guards >= from an off-by-one mutation).
    at = _ac(AcoustIDMatch(0.92, "Artist", "Title", "rid"))
    below = _ac(AcoustIDMatch(0.9199, "Artist", "Title", "rid"))
    assert all(p.confidence is Confidence.HIGH for p in blank_id(make_track(path="b.mp3"), Providers(acoustid=at)))
    assert all(p.confidence is Confidence.REVIEW for p in blank_id(make_track(path="b.mp3"), Providers(acoustid=below)))


# --- genre_fill -------------------------------------------------------------


def _dg(fake: FakeDiscogs) -> Discogs:
    return cast(Discogs, fake)


def test_genre_fill_fills_blank_from_discogs() -> None:
    dg = _dg(FakeDiscogs({("Daft Punk", "One More Time"): ["House", "Electronic"]}))
    t = make_track(artist="Daft Punk", title="One More Time")  # genre blank
    (p,) = genre_fill(t, Providers(discogs=dg))
    assert p.field == "genre" and p.proposed == "House" and p.confidence is Confidence.REVIEW


def test_genre_fill_never_overwrites_an_existing_genre() -> None:
    dg = _dg(FakeDiscogs({("Daft Punk", "One More Time"): ["House"]}))
    t = make_track(artist="Daft Punk", title="One More Time", genre="Disco")
    assert genre_fill(t, Providers(discogs=dg)) == []


def test_genre_fill_dormant_without_discogs() -> None:
    assert genre_fill(make_track(artist="A", title="B"), Providers()) == []


# --- year_fill --------------------------------------------------------------


def test_year_fill_fills_blank_from_mb_earliest_release() -> None:
    m = AcoustIDMatch(0.95, "Radiohead", "Creep", "rid-creep")
    mb = _mb(FakeMusicBrainz(years={"rid-creep": "1992"}))
    t = make_track(path="x.opus", artist="Radiohead", title="Creep")  # year blank
    (p,) = year_fill(t, Providers(acoustid=_ac(m), musicbrainz=mb))
    assert p.field == "year" and p.proposed == "1992" and p.confidence is Confidence.REVIEW


def test_year_fill_never_overwrites_an_existing_year() -> None:
    m = AcoustIDMatch(0.95, "Radiohead", "Creep", "rid-creep")
    mb = _mb(FakeMusicBrainz(years={"rid-creep": "1992"}))
    t = make_track(path="x.opus", artist="Radiohead", title="Creep", year="1993")
    assert year_fill(t, Providers(acoustid=_ac(m), musicbrainz=mb)) == []


def test_year_fill_dormant_without_fingerprint_or_mb() -> None:
    assert year_fill(make_track(year=""), Providers(musicbrainz=_mb(FakeMusicBrainz()))) == []


def test_blank_id_no_provider() -> None:
    assert blank_id(make_track(path="b.mp3"), Providers()) == []


def test_blank_id_not_blank() -> None:
    t = make_track(artist="A", title="B")
    assert blank_id(t, Providers(acoustid=_ac(AcoustIDMatch(0.99, "X", "Y", "r")))) == []


def test_blank_id_reidentifies_suspicious_placeholder_album() -> None:
    # the Karolina case: title wrong, album is a placeholder -> re-ID the wrong title
    m = AcoustIDMatch(0.97, "Karolina", "Happiness", "rid-k")
    t = make_track(artist="Karolina", title="קרולינה Happiness", album="Unknown Album]")
    (p,) = blank_id(t, Providers(acoustid=_ac(m)))
    assert p.field == "title" and p.proposed == "Happiness"
    assert p.confidence is Confidence.REVIEW  # overwriting populated data is never auto-HIGH


def test_blank_id_suspicious_but_field_matches_is_left() -> None:
    m = AcoustIDMatch(0.97, "Karolina", "Happiness", "rid-k")
    t = make_track(artist="Karolina", title="Happiness", album="Unknown")
    assert blank_id(t, Providers(acoustid=_ac(m))) == []


# --- album_junk -------------------------------------------------------------


def test_album_junk_clears_placeholder() -> None:
    (p,) = album_junk(make_track(album="Unknown Album]"), Providers())
    assert (p.field, p.proposed, p.confidence) == ("album", "", Confidence.REVIEW)


def test_album_junk_strips_stray_bracket() -> None:
    (p,) = album_junk(make_track(album="Abbey Road]"), Providers())
    assert p.proposed == "Abbey Road" and p.confidence is Confidence.HIGH


def test_album_junk_leaves_internal_unmatched_bracket() -> None:
    # an internal unclosed '(' is ambiguous (add ')' vs drop '(') -> don't auto-mangle to 'Vol. 1 Disc 2'
    assert album_junk(make_track(album="Vol. 1 (Disc 2"), Providers()) == []


def test_album_junk_strips_remaster() -> None:
    (p,) = album_junk(make_track(album="Nevermind (2011 Remaster)"), Providers())
    assert p.proposed == "Nevermind"


def test_album_junk_keeps_real_album() -> None:
    assert album_junk(make_track(album="The Dark Side of the Moon"), Providers()) == []


def test_album_junk_ignores_blank() -> None:
    assert album_junk(make_track(album=""), Providers()) == []
