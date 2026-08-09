"""Property tests: invariants over generated input, not hand-picked examples."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from tagistry import pipeline
from tagistry.domain import Confidence, Evidence, Proposal, Track, display_credits, parse_credits
from tagistry.fixers import _clean_title, flip
from tagistry.policies import VERDICTS, _drops_context, adjudicate_change
from tagistry.providers import LibraryPrior, Providers
from tagistry.scrobble import pick_scrobble_name
from tagistry.text import canonicalize, key, safe_filename, to_ascii_dashes

# Fragments so real-tag separators, dashes, and scripts (including " feat. ") appear in generated strings.
_fragment = st.sampled_from(
    [
        "a",
        "b",
        "X",
        "Z",
        " ",
        "0",
        "&",
        ",",
        ";",
        "/",
        "-",
        "–",
        "—",
        ".",
        " feat. ",
        " ft ",
        " x ",
        " vs ",
        "כ",
        "Radiohead",
    ]
)
_tag_text = st.lists(_fragment, max_size=8).map("".join)


@given(_tag_text)
def test_parse_display_is_lossless(s: str) -> None:
    # display_credits inverts parse_credits exactly: separators are captured, so round-trip holds for any string.
    assert display_credits(parse_credits(s)) == s


@given(_tag_text)
def test_to_ascii_dashes_is_idempotent(s: str) -> None:
    once = to_ascii_dashes(s)
    assert to_ascii_dashes(once) == once
    assert "–" not in once and "—" not in once


@given(_tag_text)
def test_canonicalize_is_idempotent(s: str) -> None:
    # Every written value is canonicalized, so a second pass must not change it (else apply/re-scan loops).
    once = canonicalize(s)
    assert canonicalize(once) == once


@given(_tag_text)
def test_clean_title_is_idempotent(s: str) -> None:
    # title_junk auto-applies at HIGH, so a non-fixed point eats one segment per scan+apply cycle
    _, once = _clean_title(s)
    assert _clean_title(once)[1] == once


def test_clean_title_keeps_a_number_that_is_part_of_the_title() -> None:
    for title in ("1 - 800 - 273 - 8255", "24 - 7", "01. 02. Song", "3. 6. 9"):
        _, once = _clean_title(title)
        assert _clean_title(once)[1] == once, title


@given(_tag_text)
def test_safe_filename_has_no_illegal_chars_and_is_idempotent(s: str) -> None:
    once = safe_filename(s)
    assert not (set(once) & set('<>:"/\\|?*'))  # never emits a filesystem-illegal char
    assert not once.endswith((" ", "."))  # Windows: no trailing space/dot
    assert safe_filename(once) == once


@given(st.lists(_tag_text, min_size=1, max_size=6))
def test_dedup_is_idempotent(titles: list[str]) -> None:
    # dedup keeps one proposal per (path, field); running it on its own output is a no-op.
    props = [
        Proposal(f"f{i}.mp3", "title", "cur", t or "x", Confidence.HIGH, Evidence("f", 100, "r"), "title_junk")
        for i, t in enumerate(titles)
    ]
    once = pipeline.dedup(props)
    assert pipeline.dedup(once) == once


@given(_tag_text)
def test_key_is_idempotent(s: str) -> None:
    once = key(s)
    assert key(once) == once


@given(_tag_text, st.sampled_from(["45", "x", "Some Artist", ""]))
def test_flip_never_proposes_without_musicbrainz(title: str, artist: str) -> None:
    # Flip only proposes after MusicBrainz verifies the swap, so offline it stays silent whatever the prior.
    lp = LibraryPrior.from_tracks([Track(f"k{i}.mp3", "mp3", {"artist": title, "title": ""}) for i in range(3)])
    track = Track("t.mp3", "mp3", {"artist": artist, "title": title})
    assert flip(track, Providers(library=lp)) == []


# --- scrobble-name choice law -----------------------------------------------

_spelling = st.sampled_from(["Tiesto", "Tiesto2", "Din Din Aviv", "Ye", "Kanye West", "A", "B"])
_counts = st.dictionaries(_spelling, st.integers(min_value=0, max_value=10_000_000), max_size=5)


@given(_counts, _spelling)
def test_pick_scrobble_name_result_is_current_or_a_known_spelling(counts: dict[str, int], current: str) -> None:
    choice = pick_scrobble_name(current, counts)
    assert choice.name == current or choice.name in counts  # never invents a spelling


@given(_counts, _spelling)
def test_pick_scrobble_name_never_switches_below_margin(counts: dict[str, int], current: str) -> None:
    choice = pick_scrobble_name(current, counts, margin=1.25)
    if choice.changed:
        cur_n, best_n = counts.get(current, 0), counts[choice.name]
        # a switch is justified ONLY by: current has no page, or the winner clears the margin
        assert (cur_n == 0 and best_n > 0) or best_n >= cur_n * 1.25
    else:
        assert key(choice.name) == key(current)  # not switching means keeping the current spelling


@given(_counts, _spelling)
def test_pick_scrobble_name_is_a_fixed_point(counts: dict[str, int], current: str) -> None:
    # switch to the winner, then re-decide from the winner: it must not move to yet another name
    choice = pick_scrobble_name(current, counts)
    if choice.changed:
        assert not pick_scrobble_name(choice.name, counts).changed


# --- policy predicates ------------------------------------------------------

_POLICY_TOKENS = ["Song", "Live", "Remix", "Radio", "Edit"]


@given(st.permutations(_POLICY_TOKENS))
def test_policies_reorder_is_never_a_context_drop(perm: list[str]) -> None:
    # a reorder keeps the SAME tokens, so it is never a shortening/context-loss
    assert not _drops_context(" ".join(_POLICY_TOKENS), " ".join(perm))


@given(_tag_text, _tag_text)
def test_adjudicate_change_is_total(current: str, proposed: str) -> None:
    verdict, reason = adjudicate_change(current, proposed)
    assert verdict in VERDICTS and isinstance(reason, str)
