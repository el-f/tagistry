"""Text normalizers."""

from __future__ import annotations

import time

from tagistry.text import (
    adds_version_marker,
    alnum,
    canonicalize,
    has_unicode_dash,
    has_version_marker,
    key,
    norm,
    safe_filename,
    strip_codec_marker,
    strip_media_junk,
    subset,
    to_ascii_dashes,
    to_ascii_quotes,
)


def test_norm() -> None:
    assert norm("Earth, Wind & Fire") == "earth wind and fire"
    assert norm("  A   B ") == "a b"


def test_alnum() -> None:
    assert alnum("JAY-Z") == "jayz"
    assert alnum("AC/DC") == "acdc"
    assert alnum("Simon & Garfunkel") == "simonandgarfunkel"


def test_key_keeps_commas() -> None:
    assert key("Earth, Wind & Fire") == "earth, wind and fire"


def test_subset() -> None:
    assert subset("Hall & Oates", "Daryl Hall & John Oates")
    assert not subset("Metallica", "Megadeth")
    assert not subset("", "Anything")


def test_to_ascii_dashes() -> None:
    assert to_ascii_dashes("A – B") == "A - B"  # en dash
    assert to_ascii_dashes("A—B") == "A-B"  # em dash
    assert to_ascii_dashes("5−3") == "5-3"  # minus sign
    assert to_ascii_dashes("Jay-Z") == "Jay-Z"  # ascii untouched


def test_has_unicode_dash() -> None:
    assert has_unicode_dash("Song – Live")
    assert not has_unicode_dash("Song - Live")
    assert not has_unicode_dash("plain")


def test_to_ascii_quotes() -> None:
    assert to_ascii_quotes("Don’t “Hi”") == 'Don\'t "Hi"'
    assert to_ascii_quotes("plain 'x'") == "plain 'x'"


def test_canonicalize_folds_everything() -> None:
    # fullwidth + curly quote + en dash + doubled space -> all ASCII, one space
    assert canonicalize("Da＊＊it  – Don’t") == "Da**it - Don't"


def test_alnum_folds_modifier_letter_apostrophes() -> None:
    # The okina is Unicode Lm (a LETTER), so \w keeps it while the ASCII ' is stripped.
    assert alnum("Israel Kamakawiwoʻole") == alnum("Israel Kamakawiwo'ole")
    assert alnum("Mark Kealiʻi Hoʻomalu") == alnum("Mark Keali'i Ho'omalu")


def test_match_keys_fold_accents() -> None:
    # match/compare forms must be accent-insensitive: the same name with/without accents matches.
    assert key("Antônio Carlos Jobim") == key("Antonio Carlos Jobim")
    assert alnum("Beyoncé") == alnum("Beyonce")
    assert norm("Café Tacvba") == norm("Cafe Tacvba")
    assert subset("Malaguena", "Malagueña Salerosa")  # accent-folded token subset


def test_has_version_marker() -> None:
    assert has_version_marker("Old Town Road (Remix)")
    assert has_version_marker("Africa (Live)")
    assert has_version_marker("ABC (The Reflex Revision)")
    assert not has_version_marker("Old Town Road")
    assert not has_version_marker("Africa")  # 'a' inside a word is not a marker (word boundary)


def test_adds_version_marker() -> None:
    # a marker restored on the proposed side that the current lacks -> True
    assert adds_version_marker("ABC", "ABC (The Reflex Revision)")
    assert adds_version_marker("Save Your Tears", "Save Your Tears (Remix)")
    # both sides already carry the same marker -> nothing added
    assert not adds_version_marker("Song (Live)", "Song (Live)")
    # a new marker on top of an existing different one -> True
    assert adds_version_marker("Song (Live)", "Song (Live) (Remix)")
    # neither side has a marker -> False (a plain rename is not a marker restore)
    assert not adds_version_marker("Beyonce", "Beyoncé")
    # proposed DROPS a marker -> not an add
    assert not adds_version_marker("Song (Remix)", "Song")


def test_canonicalize_keeps_accents() -> None:
    # the WRITTEN form keeps accents -- only the match form folds them
    assert canonicalize("Beyoncé") == "Beyoncé"
    assert canonicalize("Antônio") == "Antônio"


def test_strip_codec_marker() -> None:
    # codec/channel-format markers dropped
    assert strip_codec_marker("Detonation (5.1 mix)") == "Detonation"
    assert strip_codec_marker("Song (Dolby Atmos mix)") == "Song"
    assert strip_codec_marker("Song (stereo)") == "Song"
    assert strip_codec_marker("Song (mono version)") == "Song"
    assert strip_codec_marker("Trapped in the Drive-Thru (5.1 mix)") == "Trapped in the Drive-Thru"
    # a real version marker is NOT a codec marker -> kept
    assert strip_codec_marker("Song (Live)") == "Song (Live)"
    assert strip_codec_marker("Song (Kaskade remix)") == "Song (Kaskade remix)"
    # a named mix that merely contains 'stereo' is left alone (doesn't START with a codec word)
    assert strip_codec_marker("Shout (2014 Steven Wilson stereo mix)") == "Shout (2014 Steven Wilson stereo mix)"


def test_strip_media_junk() -> None:
    # video/audio-delivery cruft dropped (YouTube-rip junk, not part of the recording's title)
    assert strip_media_junk("Chemical Ride (Music Video)") == "Chemical Ride"
    assert strip_media_junk("Rich Boy (Lyric video)") == "Rich Boy"
    assert strip_media_junk("Song [Official Audio]") == "Song"
    assert strip_media_junk("Song (Official Music Video) (HD)") == "Song"
    # a real version marker without a delivery word is NOT junk -> kept
    assert strip_media_junk("Song (Live)") == "Song (Live)"
    assert strip_media_junk("Song (feat. X)") == "Song (feat. X)"
    # stripping everything would leave nothing -> keep the original rather than emit ""
    assert strip_media_junk("(Official Video)") == "(Official Video)"


def test_strip_media_junk_caps_an_adversarial_title() -> None:
    # _MEDIA_JUNK backtracks quadratically -- 16 KB of '[a' costs ~26s of CPU without the cap
    crafted = "[a" * 8000
    start = time.perf_counter()
    assert strip_media_junk(crafted) == crafted
    assert time.perf_counter() - start < 1.0


def test_safe_filename_drops_a_leading_dash() -> None:
    # '-name.mp3' is flag-shaped to a tool taking the path as a bare positional (fpcalc)
    assert not safe_filename("-rf").startswith("-")
    assert not safe_filename("--force").startswith("-")


def test_safe_filename_caps_the_stem() -> None:
    # tagio appends a 44-char '.tagistry.<uuid>.tmp' beside the file; an overlong stem breaks that
    assert len(safe_filename("x" * 400)) <= 180


def test_safe_filename_neutralises_path_separators() -> None:
    stem = safe_filename("../../.." + "/" + "sensitive")
    assert "/" not in stem and "\\" not in safe_filename(r"..\..\Windows\System32")
