"""plan_scrobble_names: retag an artist to its most-scrobbled MusicBrainz alias, per distinct artist."""

from __future__ import annotations

from typing import cast

from conftest import make_track
from fakes import FakeLastFm, FakeMusicBrainz
from tagistry import planners
from tagistry.domain import Confidence
from tagistry.providers import LastFm, MusicBrainz, Providers
from tagistry.providers.musicbrainz import ArtistIdentity


def _providers(mb: FakeMusicBrainz, lf: FakeLastFm) -> Providers:
    return Providers(musicbrainz=cast(MusicBrainz, mb), lastfm=cast(LastFm, lf))


def test_switches_all_tracks_of_an_artist_to_the_most_scrobbled_alias() -> None:
    ident = ArtistIdentity("mbid1", "Knesiyat Hasekhel", 100, ("כנסיית השכל",))
    mb = FakeMusicBrainz(identities={"Knesiyat Hasekhel": ident})
    lf = FakeLastFm({"Knesiyat Hasekhel": 97, "כנסיית השכל": 13900})
    tracks = [
        make_track(path="a.mp3", artist="Knesiyat Hasekhel"),
        make_track(path="b.mp3", artist="Knesiyat Hasekhel"),
    ]
    props = planners.plan_scrobble_names(tracks, _providers(mb, lf))
    assert len(props) == 2  # both tracks of the artist
    assert {p.track_path for p in props} == {"a.mp3", "b.mp3"}
    assert all(p.field == "artist" and p.proposed == "כנסיית השכל" and p.confidence is Confidence.REVIEW for p in props)


def test_current_tag_count_is_baseline_even_if_not_an_exact_mb_spelling() -> None:
    # 'kanye west' matches by key but is not an exact spelling, so its own count must still be fetched.
    ident = ArtistIdentity("mbid3", "Kanye West", 100, ("Ye",))
    mb = FakeMusicBrainz(identities={"kanye west": ident})
    lf = FakeLastFm({"kanye west": 9_000_000, "Kanye West": 9_000_000, "Ye": 100})
    props = planners.plan_scrobble_names([make_track(path="a.mp3", artist="kanye west")], _providers(mb, lf))
    assert props == []  # current is the most-scrobbled (tie with primary) -> no switch to 'Ye'


def test_keeps_current_when_it_is_the_most_scrobbled() -> None:
    ident = ArtistIdentity("mbid2", "Hanan Ben Ari", 100, ("חנן בן ארי",))
    mb = FakeMusicBrainz(identities={"Hanan Ben Ari": ident})
    lf = FakeLastFm({"Hanan Ben Ari": 8880, "חנן בן ארי": 3030})
    props = planners.plan_scrobble_names([make_track(path="a.mp3", artist="Hanan Ben Ari")], _providers(mb, lf))
    assert props == []  # current already dominant -> no proposal


def test_skips_when_mb_hit_is_not_the_tagged_artist() -> None:
    # identity's spellings do not include the current tag -> a wrong/fuzzy hit, don't trust aliases
    ident = ArtistIdentity("mbidX", "Some Other Band", 100, ("Alias Y",))
    mb = FakeMusicBrainz(identities={"Ambiguous Name": ident})
    lf = FakeLastFm({"Alias Y": 99999})
    props = planners.plan_scrobble_names([make_track(path="a.mp3", artist="Ambiguous Name")], _providers(mb, lf))
    assert props == []


def test_dormant_without_lastfm() -> None:
    mb = FakeMusicBrainz(identities={"X": ArtistIdentity("m", "X", 100, ("Y",))})
    providers = Providers(musicbrainz=cast(MusicBrainz, mb))  # no lastfm
    assert planners.plan_scrobble_names([make_track(path="a.mp3", artist="X")], providers) == []
