"""MusicBrainz provider, driven by recorded vcrpy cassettes (hermetic in CI)."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from tagistry.providers.musicbrainz import MusicBrainz


class _Resp:
    status_code = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _RoutedSession:
    """Search payload for the artist query; aliases payload for the mbid lookup."""

    def __init__(self, search: dict[str, object], aliases: dict[str, object]) -> None:
        self._search, self._aliases = search, aliases

    def get(
        self, url: str, params: dict[str, str] | None = None, headers: object = None, timeout: object = None
    ) -> _Resp:
        if url.endswith("/artist") and params and "query" in params:
            return _Resp(self._search)
        return _Resp(self._aliases)  # the /artist/<mbid>?inc=aliases lookup


def test_artist_identity_parses_name_mbid_and_filters_alias_types() -> None:
    search = {"artists": [{"id": "mbid1", "name": "Din Din Aviv", "score": 100}]}
    aliases = {
        "aliases": [
            {"name": "דין דין אביב", "type": "Artist name"},
            {"name": "Aviv, Din Din", "type": "Legal name"},  # not a display spelling -> dropped
        ]
    }
    mb = MusicBrainz(session=_RoutedSession(search, aliases))  # type: ignore[arg-type]
    ident = mb.artist_identity("Din Din Aviv")
    assert ident is not None
    assert (ident.mbid, ident.name, ident.score) == ("mbid1", "Din Din Aviv", 100)
    assert ident.aliases == ("דין דין אביב",)  # 'Legal name' filtered out
    assert set(ident.spellings()) == {"Din Din Aviv", "דין דין אביב"}


def test_artist_identity_none_on_empty() -> None:
    mb = MusicBrainz(session=_RoutedSession({"artists": []}, {}))  # type: ignore[arg-type]
    assert mb.artist_identity("Nobody At All") is None


@pytest.fixture
def vcr_config() -> dict[str, object]:
    return {"filter_headers": ["User-Agent"], "record_mode": "once"}


@pytest.mark.vcr
def test_artist_search_hit() -> None:
    mb = MusicBrainz(session=requests.Session())
    name, score = mb.artist_search("Radiohead")
    assert score == 100
    assert "Radiohead" in name


@pytest.mark.vcr
def test_recording_search_hit() -> None:
    mb = MusicBrainz(session=requests.Session())
    assert mb.recording_search("Karma Police", "Radiohead") >= 90


@pytest.mark.vcr
def test_recording_search_swapped_orientation() -> None:
    # "Shinedown" is not a song by "45" -> low; the flip fixer relies on this asymmetry
    mb = MusicBrainz(session=requests.Session())
    assert mb.recording_search("Shinedown", "45") < 90


@pytest.mark.vcr
def test_recording_by_id_returns_canonical() -> None:
    mb = MusicBrainz(session=requests.Session())
    result = mb.recording_by_id("3eea5cf7-feba-49bc-be94-1b155dbcb165")
    assert result is not None
    title, artist = result
    assert title == "Bohemian Rhapsody"
    assert "Queen" in artist


def test_default_session_builds(tmp_path: Path) -> None:
    mb = MusicBrainz(cache_path=tmp_path / "mbcache")
    assert mb._session is not None


@pytest.mark.vcr
def test_recording_top_returns_artist_credit() -> None:
    mb = MusicBrainz(session=requests.Session())
    top = mb.recording_top("Bohemian Rhapsody", "Queen")
    assert top is not None
    title, artist, score = top
    assert title == "Bohemian Rhapsody" and "Queen" in artist and score >= 90


class _FakeResp:
    def __init__(self, data: object) -> None:
        self._data = data
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._data


class _FakeSession:
    """Records the last query and returns a fixed body -- for offline shape/escaping tests."""

    def __init__(self, data: object) -> None:
        self._data = data
        self.last_params: dict[str, str] | None = None

    def get(
        self, url: str, params: dict[str, str] | None = None, headers: object = None, timeout: object = None
    ) -> _FakeResp:
        self.last_params = params
        return _FakeResp(self._data)


def test_recording_query_escapes_quotes() -> None:
    sess = _FakeSession({"recordings": []})
    mb = MusicBrainz(session=sess)  # type: ignore[arg-type]
    mb.recording_search('Say "Hello"', "Artist")
    query = (sess.last_params or {})["query"]
    assert '\\"Hello\\"' in query  # inner quotes escaped, so the Lucene phrase doesn't break -> no 400


def test_recording_year_returns_earliest_release() -> None:
    body = {"releases": [{"date": "1998-05-01"}, {"date": "1992"}, {"date": ""}, {"nope": 1}]}
    mb = MusicBrainz(session=_FakeSession(body))  # type: ignore[arg-type]
    assert mb.recording_year("rid") == "1992"  # earliest wins; a comp/reissue can't override


def test_recording_year_none_without_dated_releases() -> None:
    assert MusicBrainz(session=_FakeSession({"releases": []})).recording_year("rid") is None  # type: ignore[arg-type]
    assert MusicBrainz(session=_FakeSession({})).recording_year("rid") is None  # type: ignore[arg-type]


def test_search_handles_non_dict_element() -> None:
    # a malformed MB body (null element in the list) must not crash with AttributeError
    mb_a = MusicBrainz(session=_FakeSession({"artists": [None]}))  # type: ignore[arg-type]
    assert mb_a.artist_search("x") == ("", 0)
    mb_r = MusicBrainz(session=_FakeSession({"recordings": [None]}))  # type: ignore[arg-type]
    assert mb_r.recording_search("t", "a") == 0
    assert mb_r.recording_top("t") is None
