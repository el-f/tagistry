"""last.fm provider: scrobble counts drive the canonical-name choice."""

from __future__ import annotations

from tagistry.providers.lastfm import LastFm, LastFmPage


def _lf(url_to_json: dict[str, object]) -> LastFm:
    def getter(url: str) -> object:
        for needle, payload in url_to_json.items():
            if needle in url:
                return payload
        return None

    return LastFm("key", getter)  # type: ignore[arg-type]


def test_listeners_parses_stats() -> None:
    lf = _lf({"artist.getInfo": {"artist": {"name": "X", "stats": {"listeners": "8880", "playcount": "242600"}}}})
    assert lf.listeners("X") == 8880


def test_listeners_none_when_unknown() -> None:
    assert _lf({}).listeners("Nobody") is None  # getter returns None


def test_listeners_none_on_malformed() -> None:
    assert _lf({"artist.getInfo": {"artist": {"stats": {"listeners": "n/a"}}}}).listeners("X") is None


def test_scrobble_counts_dedups_and_skips_unknown() -> None:
    def getter(url: str) -> object:
        if "artist=A" in url:
            return {"artist": {"stats": {"listeners": "100"}}}
        return None

    lf = LastFm("key", getter)  # type: ignore[arg-type]
    assert lf.scrobble_counts(["A", "B", "A", ""]) == {"A": 100}  # B unknown, blank/dupe dropped


# --- keyless page source ---

_PAGE_HTML = (
    '<abbr class="intabbr js-abbreviated-counter" title="8,345,042">8.3M</abbr>'  # listeners (first)
    '<abbr class="intabbr js-abbreviated-counter" title="1,405,315,855">1.41B</abbr>'  # scrobbles
)


def test_page_listeners_parses_first_counter() -> None:
    assert LastFmPage(lambda url: _PAGE_HTML).listeners("Radiohead") == 8_345_042


def test_page_listeners_none_when_missing_or_404() -> None:
    assert LastFmPage(lambda url: "<html>not found</html>").listeners("Nobody") is None
    assert LastFmPage(lambda url: None).listeners("X") is None  # 404 / network error


def test_page_scrobble_counts_uses_shared_loop() -> None:
    pages = {"A": _PAGE_HTML, "B": "no counter here"}
    lf = LastFmPage(lambda url: pages.get(url.rsplit("/", 1)[-1]))
    assert lf.scrobble_counts(["A", "B", ""]) == {"A": 8_345_042}  # B has no page count, blank skipped
