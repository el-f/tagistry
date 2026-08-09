"""Shared text normalizers. Pure, property-tested."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^\w]", re.UNICODE)

# Hyphen, non-breaking hyphen, figure dash, en/em dash, horizontal bar, minus sign
_DASH_TABLE = dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212), "-")

# Lm apostrophes (okina): `\w` keeps them but strips ASCII '. Not all of Lm -- that eats U+30FC.
_MODIFIER_APOSTROPHES = dict.fromkeys((0x02B9, 0x02BB, 0x02BC, 0x02BD, 0x02BE, 0x02BF), None)


def to_ascii_dashes(s: str) -> str:
    """Replace every non-ASCII dash with an ASCII hyphen."""
    return s.translate(_DASH_TABLE)


def has_unicode_dash(s: str) -> bool:
    return any(ord(c) in _DASH_TABLE for c in s)


# Curly quotes / apostrophes / primes -> straight ASCII (matches the ASCII-dash preference).
_QUOTE_TABLE = {
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x201B: "'",
    0x2032: "'",
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x201F: '"',
    0x2033: '"',
}


def to_ascii_quotes(s: str) -> str:
    return s.translate(_QUOTE_TABLE)


def canonicalize(s: str) -> str:
    """The universal safe fold to the canonical ASCII match form: fullwidth -> ASCII,
    unicode dashes -> '-', curly quotes -> straight, runs of whitespace -> one space.
    Applied to every value the pipeline writes, so any fixer's output comes out canonical."""
    return _WS.sub(" ", to_ascii_quotes(to_ascii_dashes(fold_fullwidth(s)))).strip()


def fold_fullwidth(s: str) -> str:
    """Fold fullwidth ASCII-variant chars to ASCII: ＊ -> *, ＂ -> ", ／ -> /, etc.

    Windows filenames can't hold * " / : ? < > |, so tools substitute the fullwidth
    forms (U+FF01-FF5E); those leak into tags and break exact matching against
    last.fm / MusicBrainz. Also folds the ideographic space (U+3000) to a normal space.

    NOT unicodedata.normalize('NFKC'): NFKC over-normalizes the WRITTEN value -- H2O with a
    subscript -> H2O, the fraction half -> '1/2', the fi ligature -> 'fi', roman numeral IV -> 'IV'.
    Those are real title characters, so we fold only the fullwidth block here. (Matching uses NFKD
    via fold_accents, which can be aggressive because it never touches what's written.)
    """
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# Separators and colon become '-' so "AC/DC" stays readable; accents are kept (NTFS holds Unicode)
_FN_TO_DASH = re.compile(r"[/\\:|]")
_FN_DROP = re.compile(r'[<>"?*\x00-\x1f]')
# Leaves room for the 44-char '.tagistry.<uuid>.tmp' suffix tagio adds under the same directory.
_FN_MAX = 180


def safe_filename(name: str) -> str:
    """Turn a tag value into a filesystem-safe filename stem (no extension). Keeps accents.

    A leading '-' is dropped so the result is never flag-shaped to a tool we hand it to (fpcalc
    takes the path as a bare positional), and the stem is capped so the rename cannot produce a
    path too long for the atomic tag write that follows it.
    """
    name = _FN_DROP.sub("", _FN_TO_DASH.sub("-", name))
    return _WS.sub(" ", name).strip().lstrip("-. ").rstrip(" .")[:_FN_MAX].rstrip(" .")


def fold_accents(s: str) -> str:
    """Drop combining marks (Latin accents, Hebrew niqqud) for MATCHING only: 'Antônio' ->
    'Antonio', 'Beyoncé' -> 'Beyonce'. NFKD also folds fullwidth/compatibility forms, so the
    compare form matches across those too. Written tags keep their accents (see canonicalize)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    """Loose compare form: accent-folded, lowercase, & -> and, commas dropped, spaces collapsed."""
    s = fold_accents(s).lower().replace("&", "and").replace(",", " ")
    return _WS.sub(" ", s).strip()


def alnum(s: str) -> str:
    """Strip everything but letters/digits, accent-folded, so 'JAY-Z' == 'jayz', 'Beyoncé' == 'beyonce'."""
    return _NONWORD.sub("", fold_accents(s).translate(_MODIFIER_APOSTROPHES).lower().replace("&", "and"))


def key(s: str) -> str:
    """Match key: accent-folded, lowercase, & -> and, spaces collapsed. Commas kept (vs norm)."""
    return _WS.sub(" ", fold_accents(s).lower().replace("&", "and")).strip()


# A marker distinguishes a recording from the plain original (remix, live take, edit, cover)
VERSION_MARKER_RE = re.compile(
    r"\b(remix|revision|rework|edit|version|instrumental|acoustic|live|demo|bootleg|mashup|"
    r"unplugged|remaster(?:ed)?|reprise|radio|extended|session|cover|flip|dub|vip|mix|feat|ft)\b",
    re.IGNORECASE,
)


def _markers(s: str) -> set[str]:
    return {m.group(0).lower() for m in VERSION_MARKER_RE.finditer(fold_accents(s).lower())}


def has_version_marker(s: str) -> bool:
    """True when the string carries any version marker (remix/live/edit/...)."""
    return bool(VERSION_MARKER_RE.search(fold_accents(s).lower()))


def adds_version_marker(current: str, proposed: str) -> bool:
    """True when proposed carries a version marker keyword that current lacks -- the shape of a
    fingerprint restoring a marker the tags had lost ('ABC' -> 'ABC (The Reflex Revision)')."""
    return bool(_markers(proposed) - _markers(current))


# Only a trailing group STARTING with one of these; a named mix containing 'stereo' is left alone
_CODEC_GROUP = re.compile(
    r"\s*[\(\[]\s*(?:5\.1|7\.1|dolby ?atmos|atmos|surround|quadraphonic|quad|dts|binaural|"
    r"spatial|stereo|mono)\b[^()\[\]]*[)\]]\s*$",
    re.IGNORECASE,
)


def strip_codec_marker(title: str) -> str:
    """Drop trailing codec/channel-format markers from a title.
    'Detonation (5.1 mix)' -> 'Detonation'; 'Song (Live)' unchanged."""
    s = title
    while (stripped := _CODEC_GROUP.sub("", s)) != s:  # one sub per pass, not two
        s = stripped
    return s.strip() or title


_MAX_TITLE_SCAN = 500  # no real title is longer; past this _MEDIA_JUNK's backtracking dominates

# Junk only when the group names a delivery format, so '(Music Box Version)' survives
_MEDIA_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*"
    r"(?:official|lyric|visualizer|explicit|\bhd\b|\bhq\b|\b4k\b|music\s+video|\bvideo\b|\baudio\b)"
    r"[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)


def strip_media_junk(title: str) -> str:
    """Drop video/audio-delivery junk groups from a title ('(Official Video)', '(Lyric video)').
    Shared by title_junk (clean the tag) and canonicalize (don't ADD junk carried in AcoustID's
    raw metadata title). Returns the original if stripping would leave nothing."""
    # _MEDIA_JUNK backtracks quadratically -- a crafted 16 KB title costs ~26s of CPU
    if len(title) > _MAX_TITLE_SCAN:
        return title
    stripped = _WS.sub(" ", _MEDIA_JUNK.sub(" ", title)).strip()
    return stripped or title


def tokens(s: str) -> set[str]:
    return set(re.findall(r"\w+", norm(s)))


def subset(query: str, name: str) -> bool:
    """True when query's tokens are all inside name's (same act, fuller name)."""
    q, n = tokens(query), tokens(name)
    return bool(q) and q <= n
