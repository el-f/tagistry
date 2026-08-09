"""Domain models: artist-credit parse/display round-trip and helpers."""

from __future__ import annotations

import pytest

from tagistry.domain import (
    REQUIRED_REVIEW_COLUMNS,
    REVIEW_COLUMNS,
    ArtistCredit,
    Confidence,
    ReviewRow,
    display_credits,
    is_probably_band,
    parse_credits,
    primary_credit,
)

ROUND_TRIP = [
    "Radiohead",
    "Earth, Wind & Fire",
    "Kool & The Gang",
    "Simon & Garfunkel",
    "Jay-Z",
    "AC/DC",
    "Wham!",
    "Tokyo Tears, Carlos Vergel Carreto",
    "Calvin Harris feat. Rihanna",
    "Drake ft. Future",
    "Angus & Julia Stone",
    "Above & Beyond vs. Andy Moor",
    "Sonny Rollins / Coleman Hawkins",
]


@pytest.mark.parametrize("s", ROUND_TRIP)
def test_parse_display_round_trip(s: str) -> None:
    assert display_credits(parse_credits(s)) == s


def test_parse_captures_join_phrase() -> None:
    credits = parse_credits("Calvin Harris feat. Rihanna")
    assert [c.credited_name for c in credits] == ["Calvin Harris", "Rihanna"]
    assert credits[0].is_feat_join()
    assert credits[-1].join_phrase == ""


def test_parse_empty() -> None:
    assert parse_credits("") == []


def test_primary_credit() -> None:
    assert primary_credit(parse_credits("Tokyo Tears, Carlos Vergel Carreto")) == "Tokyo Tears"
    assert primary_credit(parse_credits("Drake ft. Future")) == "Drake"
    assert primary_credit([]) == ""


def test_is_probably_band() -> None:
    assert is_probably_band("Artist vs. Poet")
    assert is_probably_band("Katrina & The Waves")
    assert not is_probably_band("Tokyo Tears, Carlos Vergel Carreto")


def test_evidence_str_is_reason() -> None:
    from tagistry.domain import Evidence

    assert str(Evidence("flip", 100, "MB swapped=100 current=0")) == "MB swapped=100 current=0"


def test_confidence_values() -> None:
    assert {c.value for c in Confidence} == {"HIGH", "REVIEW", "LOW"}


def test_artist_credit_defaults() -> None:
    c = ArtistCredit("X")
    assert c.canonical_name is None and c.mbid is None and c.join_phrase == ""


# --- ReviewRow --------------------------------------------------------------


def test_review_columns_match_dataclass_fields() -> None:
    # the file_* context columns trail the load-bearing 8 so an old CSV keeps its column positions
    assert REVIEW_COLUMNS == (
        "apply",
        "fixer",
        "confidence",
        "path",
        "field",
        "current",
        "proposed",
        "evidence",
        "file_artist",
        "file_title",
    )
    # the file_* columns are optional on read; the rest are required (an old CSV predates them)
    assert REQUIRED_REVIEW_COLUMNS == (
        "apply",
        "fixer",
        "confidence",
        "path",
        "field",
        "current",
        "proposed",
        "evidence",
    )


def test_review_row_round_trips_through_dict() -> None:
    vals = ("apply", "flip", "HIGH", "x.mp3", "title", "A", "B", "ev", "The Artist", "The Song")
    d = dict(zip(REVIEW_COLUMNS, vals, strict=True))
    row = ReviewRow.from_dict(d)
    assert row.field == "title" and row.proposed == "B"
    assert row.file_artist == "The Artist" and row.file_title == "The Song"
    assert row.to_dict() == d


def test_review_row_from_dict_fills_missing_columns_blank() -> None:
    row = ReviewRow.from_dict({"path": "x.mp3", "proposed": "B"})
    assert row.path == "x.mp3" and row.proposed == "B" and row.fixer == "" and row.apply == ""


def test_review_row_is_apply_is_case_and_space_insensitive() -> None:
    assert ReviewRow.from_dict({"apply": " Apply "}).is_apply
    assert not ReviewRow.from_dict({"apply": "skip"}).is_apply
    assert not ReviewRow.from_dict({}).is_apply
