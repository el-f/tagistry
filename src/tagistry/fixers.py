"""The tag-correction fixers. Each is pure: (track, providers) -> [Proposal].

Fixers never touch files. They emit Proposals with confidence + evidence; the
pipeline stages them for review and applies only what the user keeps.

Each fixer self-registers via @fixer(priority) (name = the function name).
Priority is the single source of truth for both run order and dedup tie-breaking
(lower wins ties) — adding a fixer is one decorator, no edits elsewhere.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from .checks import ARTIST_MATCH_MIN, RECORDING_MATCH_MIN, Checks
from .domain import (
    Confidence,
    Proposal,
    Track,
    is_probably_band,
    make_proposal,
    parse_credits,
    primary_credit,
)
from .providers import Providers
from .research import ResearchAnswer, ResearchQuestion
from .text import (
    alnum,
    fold_fullwidth,
    has_unicode_dash,
    key,
    norm,
    strip_codec_marker,
    strip_media_junk,
    subset,
    to_ascii_dashes,
)
from .text import canonicalize as _to_canonical

type Fixer = Callable[[Track, Providers], list[Proposal]]

logger = logging.getLogger(__name__)

_ACOUSTID_AUTOAPPLY_MIN = 0.92  # below this a fingerprint fill is REVIEW, never an auto-overwrite
# Looser than RECORDING_MATCH_MIN: a flip also needs a name-match, so this is only the first filter
_FLIP_RECORDING_MIN = 90


@dataclass(frozen=True, slots=True)
class RegisteredFixer:
    name: str
    priority: int
    fn: Fixer


REGISTRY: dict[str, RegisteredFixer] = {}


def fixer(priority: int) -> Callable[[Fixer], Fixer]:
    """Register a fixer (name = its function name) with a dedup/run priority.

    Lower priority wins dedup ties and runs first.
    """

    def register(fn: Fixer) -> Fixer:
        REGISTRY[fn.__name__] = RegisteredFixer(fn.__name__, priority, fn)
        return fn

    return register


# --- multi_artist -----------------------------------------------------------

_MULTI = re.compile(
    r"[;,/]|\s&\s|\sx\s|\svs\b|\bfeat\b|\bfeat\.|\bfeaturing\b|\bft\.?\b|\bpresents\b|\bpres\.", re.IGNORECASE
)
_PAREN = re.compile(r"[\(\[].*?[\)\]]")


def _primary_of(artist: str) -> str:
    return primary_credit(parse_credits(_PAREN.sub(" ", artist)))


@fixer(priority=4)
def multi_artist(track: Track, providers: Providers) -> list[Proposal]:
    artist = track.get("artist")
    if not artist or not _MULTI.search(artist):
        return []
    prim = _primary_of(artist)
    mb = providers.musicbrainz

    if mb is None:
        lp = providers.library
        if lp is not None and lp.is_single_act(artist):
            return []  # KEEP: backing band, curated act, or a co-lead of known library artists
        if prim and norm(prim) != norm(artist) and not is_probably_band(artist):
            return [
                _p(track, "artist", artist, prim, Confidence.REVIEW, "multi_artist", 0, "offline: unverified split")
            ]
        return []

    name, score = mb.artist_search(artist)
    if score >= ARTIST_MATCH_MIN and (alnum(name) == alnum(artist) or subset(artist, name)):
        return []  # KEEP: registered act (real band or fuller official name)
    if Checks(mb).is_collaboration(artist):
        return []  # KEEP: "A & B" co-lead collaboration (The Weeknd & Ariana Grande)
    if not prim or norm(prim) == norm(artist):
        return []
    if is_probably_band(artist):
        ev = f'backing-band pattern; MB full "{name}" {score}'
        return [_p(track, "artist", artist, prim, Confidence.REVIEW, "multi_artist", score, ev)]
    # REVIEW even when MB confirms the primary -- that says nothing about the DROPPED name
    pname, pscore = mb.artist_search(prim)
    if pscore >= ARTIST_MATCH_MIN and alnum(pname) == alnum(prim):
        ev = f'MB primary "{pname}" {pscore}'
        return [_p(track, "artist", artist, prim, Confidence.REVIEW, "multi_artist", pscore, ev)]
    ev = f'primary "{prim}" MB top "{pname}" {pscore}'
    return [_p(track, "artist", artist, prim, Confidence.LOW, "multi_artist", pscore, ev)]


# --- feat_to_title ----------------------------------------------------------

_FEAT_SPLIT = re.compile(r"\s+[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
_TITLE_HAS_FEAT = re.compile(r"\b(?:feat|ft|featuring)\b", re.IGNORECASE)


def _move_feat_to_title(artist: str, title: str) -> tuple[str, str]:
    """If the artist carries a feat, return (primary, title-with-'(feat. X)'). No-op when
    there's no feat, no title to move it to, or the title already marks it."""
    parts = _FEAT_SPLIT.split(artist, 1)
    if len(parts) < 2 or not title:
        return artist, title
    primary = parts[0].strip().rstrip("([ ").strip()
    featured = parts[1].strip().rstrip(")] ").strip()
    if not primary or not featured:
        return artist, title
    if _TITLE_HAS_FEAT.search(title):
        # Strip only when the title credits the SAME guest, else a different guest is lost
        return (primary, title) if subset(featured, title) else (artist, title)
    return primary, f"{title} (feat. {featured})"


@fixer(priority=2)
def feat_to_title(track: Track, providers: Providers) -> list[Proposal]:
    """Move a featured artist out of the artist field into the title as '(feat. X)'.

    The main act belongs in artist, the feature in the title (last.fm/MusicBrainz
    convention). This also keeps a feat/remix version distinct from the original by its
    title. Emits two rows applied atomically, so the feat is never dropped by a half-apply.
    """
    artist, title = track.get("artist"), track.get("title")
    new_artist, new_title = _move_feat_to_title(artist, title)
    if new_artist == artist:
        return []
    # A band name merely reading like 'A feat. B' would corrupt both fields, so HIGH needs MB
    mb = providers.musicbrainz
    conf, ev = Confidence.HIGH, "feat belongs in the title"
    if mb is not None:
        score = mb.recording_search(title, new_artist)
        if score < RECORDING_MATCH_MIN:
            conf, ev = Confidence.REVIEW, f"feat belongs in the title (MB unverified: {score})"
    props = [_p(track, "artist", artist, new_artist, conf, "feat_to_title", 100, ev)]
    if new_title != title:
        props.append(_p(track, "title", title, new_title, conf, "feat_to_title", 100, ev))
    return props


# --- resolve_artist ---------------------------------------------------------


@fixer(priority=9)
def resolve_artist(track: Track, providers: Providers) -> list[Proposal]:
    """When the artist field is not a real artist (junk / a soundtrack name), recover the
    canonical artist from the title via MusicBrainz. Layer 1 (deterministic checks) first;
    the pluggable researcher is asked only for the residue those can't settle. REVIEW."""
    artist, title = track.get("artist"), track.get("title")
    mb = providers.musicbrainz
    if not artist.strip() or not title.strip() or mb is None:
        return []
    checks = Checks(mb)
    if checks.is_real_artist(artist) or checks.is_collaboration(artist):
        return []  # artist is already real — nothing to resolve
    found = checks.artist_for_title(title)
    if found and key(found) != key(artist):
        ev = f'artist not in MB; title "{title}" -> "{found}"'
        return [_p(track, "artist", artist, found, Confidence.REVIEW, "resolve_artist", 90, ev)]
    # residue the deterministic checks can't settle (cross-script, obscure) -> ask the agent
    answer = _ask_researcher(providers, track)
    if answer and answer.is_usable and answer.value and key(answer.value) != key(artist):
        ev = f"researcher: {answer.reasoning[:60]} ({', '.join(answer.sources[:2])})"
        return [_p(track, "artist", artist, answer.value, Confidence.REVIEW, "resolve_artist", 80, ev)]
    return []


def _ask_researcher(providers: Providers, track: Track) -> ResearchAnswer | None:
    q = ResearchQuestion(
        kind="artist_for_title",
        ask=f"What is the canonical recording artist for the track titled '{track.get('title')}' "
        f"currently tagged with artist '{track.get('artist')}'? Cite a source or answer uncertain.",
        context={"artist": track.get("artist"), "title": track.get("title"), "path": track.path},
    )
    try:
        return providers.researcher.resolve(q)
    except Exception as exc:
        logger.debug("researcher failed for %s: %s", track.path, exc)
        return None


# --- flip -------------------------------------------------------------------


@fixer(priority=3)
def flip(track: Track, providers: Providers) -> list[Proposal]:
    artist, title = track.get("artist"), track.get("title")
    lp = providers.library
    if not title.strip() or lp is None:
        return []
    if not (lp.title_is_known_artist(track) and not lp.is_known_artist(artist)):
        return []
    mb = providers.musicbrainz
    if mb is None:
        return []  # a flip swaps two fields — too high-stakes to propose on an unverified offline guess
    swapped = mb.recording_search(artist, title)  # is `artist` a song by `title`?
    current = mb.recording_search(title, artist)
    if swapped < _FLIP_RECORDING_MIN or swapped <= current:
        return []
    # A score alone lies -- MB scores a fuzzy near-miss high, so require a real name-match too
    top = mb.recording_top(artist, title)
    confirmed = bool(
        top and alnum(top[0]) == alnum(artist) and (alnum(top[1]) == alnum(title) or subset(title, top[1]))
    )
    if not confirmed:
        return []
    conf = Confidence.HIGH if current < 70 else Confidence.REVIEW
    return _swap(track, artist, title, conf, swapped, f"MB swapped={swapped} current={current}")


def _swap(track: Track, artist: str, title: str, conf: Confidence, score: int, ev: str) -> list[Proposal]:
    return [
        _p(track, "artist", artist, title, conf, "flip", score, ev),
        _p(track, "title", title, artist, conf, "flip", score, ev),
    ]


# --- merged_field -----------------------------------------------------------

_DASH = re.compile(r"\s+[-–—]\s+")  # spaced dash only; keeps "Jay-Z", "Wham!"


@fixer(priority=1)
def merged_field(track: Track, providers: Providers) -> list[Proposal]:
    artist, title = track.get("artist"), track.get("title")
    if not artist or not _DASH.search(title):
        return []
    head, tail = (s.strip() for s in _DASH.split(title, 1))
    if key(artist) == key(head) and tail:
        ev = f"title repeats artist '{artist}'"
        return [_p(track, "title", title, tail, Confidence.HIGH, "merged_field", 100, ev)]
    return []


# --- title_junk -------------------------------------------------------------

_TRACKNUM = re.compile(r"^\s*\d{1,3}\s*[.)\-]\s+")
_REMASTER_PAREN = re.compile(r"\s*[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]\s*", re.IGNORECASE)
# End-anchored; a bare trailing "Remaster" needs a dash/year/qualifier to count as junk
_REMASTER_TAIL = re.compile(
    r"\s*(?:"
    r"[-–]\s*(?:\d{4}\s+)?(?:digitally?\s+)?(?:deluxe\s+)?remaster(?:ed)?"  # dash-separated tag
    r"|\d{4}\s+(?:digitally?\s+)?(?:deluxe\s+)?remaster(?:ed)?"  # year-led tag
    r"|(?:digitally?|deluxe)\s+remaster(?:ed)?"  # qualifier-led tag
    r"|remastered"  # bare "-ed" participle
    r")(?:\s+(?:version|edition|mix|mono|stereo))?\s*$",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")
# Stripping down to only these words (plus a year) leaves no real title -> skip the strip
_REMASTER_META = {
    "digital",
    "digitally",
    "deluxe",
    "remaster",
    "remastered",
    "version",
    "edition",
    "mono",
    "stereo",
    "anniversary",
    "expanded",
    "mix",
    "remix",
}


def _has_real_title(s: str) -> bool:
    """True if s has a token that is neither a year/number nor pure remaster metadata -- i.e.
    the strip left an actual title, not just '2011 Digital'."""
    return any(not tok.isdigit() and tok.lower() not in _REMASTER_META for tok in s.split())


def _clean_title(t: str) -> tuple[str | None, str]:
    new, fixes = t, []
    stripped = _TRACKNUM.sub("", new).strip()
    # A digit-leading remainder means the number was part of the title ("1 - 800 - 273 - 8255")
    if _TRACKNUM.search(new) and stripped and not stripped[0].isdigit():
        new = stripped
        fixes.append("tracknum")
    if re.search(r"remaster", new, re.IGNORECASE):
        after = _REMASTER_TAIL.sub("", _REMASTER_PAREN.sub(" ", new)).strip(" -–")
        if after and after != new and _has_real_title(after):
            new = _WS.sub(" ", after).strip()
            fixes.append("remaster")
    after = strip_media_junk(new)
    if after != new:  # strip_media_junk returns the original if it would leave nothing
        new = after
        fixes.append("junk")
    if fixes and new and new != t:
        return "+".join(fixes), new
    return None, t


@fixer(priority=5)
def title_junk(track: Track, providers: Providers) -> list[Proposal]:
    title = track.get("title")
    if not title:
        return []
    fix, new = _clean_title(title)
    if fix is None:
        return []
    return [_p(track, "title", title, new, Confidence.HIGH, "title_junk", 100, f"strip {fix}")]


# --- album_junk -------------------------------------------------------------

_PLACEHOLDER_ALBUM = {"unknown album", "unknown", "various", "various artists", "va", "untitled"}


def _clean_album(album: str) -> str:
    """Strip remaster tags and dangling unmatched brackets from an album name.

    Unlike title junk, album parentheticals are usually meaningful editions
    ("(Deluxe Edition)", "(Official Collector's Edition)") and are kept.
    """
    new = _REMASTER_PAREN.sub(" ", album)
    if new.count("[") != new.count("]") or new.count("(") != new.count(")") or new.count("{") != new.count("}"):
        # Edges only: an internal unmatched bracket ('Vol. 1 (Disc 2') is ambiguous, so leave it
        new = new.strip("[](){}")
    return _WS.sub(" ", new).strip()


def _is_placeholder_album(album: str) -> bool:
    return bool(album.strip()) and key(_clean_album(album)) in _PLACEHOLDER_ALBUM


@fixer(priority=6)
def album_junk(track: Track, providers: Providers) -> list[Proposal]:
    """Clear placeholder albums ('Unknown Album') and strip junk/stray brackets."""
    album = track.get("album")
    if not album.strip():
        return []
    if _is_placeholder_album(album):
        return [_p(track, "album", album, "", Confidence.REVIEW, "album_junk", 100, "placeholder album -> clear")]
    new = _clean_album(album)
    if new and new != album:
        return [_p(track, "album", album, new, Confidence.HIGH, "album_junk", 100, "strip album junk")]
    return []


# --- blank_id (identify blank OR suspicious files) --------------------------


@fixer(priority=0)
def blank_id(track: Track, providers: Providers) -> list[Proposal]:
    """Fingerprint files that are blank or suspicious (placeholder album), fill from AcoustID.

    Blank fields are filled (HIGH on a strong match). A file that is populated but
    suspicious gets its differing fields proposed at REVIEW only - never auto-overwrite.
    """
    artist, title = track.get("artist"), track.get("title")
    artist_blank, title_blank = not artist.strip(), not title.strip()
    suspicious = _is_placeholder_album(track.get("album"))
    if not (artist_blank or title_blank or suspicious):
        return []
    ac = providers.acoustid
    if ac is None:
        return []
    match = ac.identify(track.path)
    if match is None:
        return []
    score = int(match.score * 100)
    # HIGH only when AcoustID's own metadata and the MB canonical agree; a disagreement -> REVIEW
    fill_artist, fill_title, agree = match.artist, match.title, True
    mb = providers.musicbrainz
    if mb is not None and match.recording_id:
        rec = mb.recording_by_id(match.recording_id)
        if rec:
            mb_title, mb_artist = rec
            agree = key(mb_artist) == key(match.artist) and key(mb_title) == key(match.title)
            fill_artist, fill_title = mb_artist or match.artist, mb_title or match.title
    ev = f"AcoustID {match.score:.2f} rid={match.recording_id}" + ("" if agree else " (AcoustID/MB differ)")
    strong = Confidence.HIGH if (match.score >= _ACOUSTID_AUTOAPPLY_MIN and agree) else Confidence.REVIEW
    props: list[Proposal] = []
    for field, value, matched, blank in (
        ("artist", artist, fill_artist, artist_blank),
        ("title", title, fill_title, title_blank),
    ):
        if blank:
            props.append(_p(track, field, value, matched, strong, "blank_id", score, ev))
        elif suspicious and matched and key(value) != key(matched):
            props.append(_p(track, field, value, matched, Confidence.REVIEW, "blank_id", score, ev))
    return props


# --- canonicalize (fingerprint -> MusicBrainz canonical recording) ----------


@fixer(priority=10)
def canonicalize(track: Track, providers: Providers) -> list[Proposal]:
    """Rewrite artist/title to the MusicBrainz canonical form for the fingerprinted recording.

    Only the AUDIO can tell that a file is the remix / feat / live version when the tags
    say the plain title. AcoustID identifies the exact recording; MusicBrainz gives its
    canonical name, whose title carries the version marker ('(Remix)', '(feat. X)', '(Live)').
    REVIEW: it overwrites existing tags, so it never auto-applies. Needs --fingerprint.
    """
    ac = providers.acoustid
    if ac is None:
        return []
    match = ac.identify(track.path)
    if match is None:
        return []
    title, artist = match.title, match.artist
    mb = providers.musicbrainz
    if mb is not None and match.recording_id:
        rec = mb.recording_by_id(match.recording_id)
        if rec:
            title, artist = rec
    # Keep canonicalize agreeing with feat_to_title, so the two never fight over the same file
    artist, title = _move_feat_to_title(artist, title)
    title = strip_codec_marker(title)  # (5.1 mix)/(stereo)/(mono) describe the mix format, not a different recording
    # canonicalize restores a real version marker; it must not import AcoustID's YouTube-rip junk
    title = strip_media_junk(title)
    artist, title = _to_canonical(artist), _to_canonical(title)
    score = int(match.score * 100)
    ev = f"AcoustID {match.score:.2f} -> MB recording {match.recording_id}"
    props: list[Proposal] = []
    for field, current, canonical in (
        ("artist", track.get("artist"), artist),
        ("title", track.get("title"), title),
    ):
        if canonical and key(current) != key(canonical):
            props.append(_p(track, field, current, canonical, Confidence.REVIEW, "canonicalize", score, ev))
    return props


# --- genre_fill (Discogs curated genres) ------------------------------------


@fixer(priority=11)
def genre_fill(track: Track, providers: Providers) -> list[Proposal]:
    """Fill a BLANK genre from Discogs' curated genres/styles for the (artist, title). Discogs
    genres are hand-curated, unlike free-text last.fm tags. REVIEW: genre is subjective, so a human
    confirms. Only fills blanks, never overwrites. Needs a $DISCOGS_TOKEN (the discogs provider)."""
    if track.get("genre").strip():
        return []
    artist, title = track.get("artist").strip(), track.get("title").strip()
    dg = providers.discogs
    if dg is None or not artist or not title:
        return []
    genres = dg.genres(artist, title)
    if not genres:
        return []
    value = genres[0]  # most-specific (style) first
    ev = f"Discogs: {', '.join(genres[:3])}"
    return [_p(track, "genre", "", value, Confidence.REVIEW, "genre_fill", 80, ev)]


# --- year_fill (fingerprint -> MusicBrainz earliest release) ----------------


@fixer(priority=12)
def year_fill(track: Track, providers: Providers) -> list[Proposal]:
    """Fill a BLANK year from the fingerprinted recording's earliest MusicBrainz release. REVIEW.
    Only fills blanks. Needs --fingerprint (AcoustID) + --online (MusicBrainz); the AcoustID lookup
    is memoized, so it shares blank_id/canonicalize's fingerprint of the same file."""
    if track.get("year").strip():
        return []
    ac, mb = providers.acoustid, providers.musicbrainz
    if ac is None or mb is None:
        return []
    match = ac.identify(track.path)
    if match is None or not match.recording_id:
        return []
    year = mb.recording_year(match.recording_id)
    if not year:
        return []
    return [
        _p(
            track,
            "year",
            "",
            year,
            Confidence.REVIEW,
            "year_fill",
            int(match.score * 100),
            f"MB earliest release {year}",
        )
    ]


# --- ascii_dash -------------------------------------------------------------


@fixer(priority=8)
def ascii_dash(track: Track, providers: Providers) -> list[Proposal]:
    """Normalize non-ASCII dashes (en/em/etc.) to ASCII '-' in artist and title."""
    props: list[Proposal] = []
    for field in ("artist", "title"):
        value = track.get(field)
        if value and has_unicode_dash(value):
            new = to_ascii_dashes(value)
            props.append(_p(track, field, value, new, Confidence.HIGH, "ascii_dash", 100, "unicode dash -> ascii"))
    return props


# --- normalize (fold to the canonical ASCII form for matching) --------------

_MULTISPACE = re.compile(r" {2,}")
# 2+ asterisks between word chars ("Da * * it"), so a single-asterisk "5 * 3" is left alone
_SPACED_CENSOR = re.compile(r"(?<=\w)\s*\*(?:\s*\*)+\s*(?=\w)")


@fixer(priority=7)
def normalize(track: Track, providers: Providers) -> list[Proposal]:
    """Fold artist/title to canonical ASCII for matching: fullwidth punctuation ->
    ASCII (＊ -> *), tighten spaced-out censored words, collapse runs of spaces. The tag
    then matches the canonical last.fm/MusicBrainz entry so scrobbles stop orphaning."""
    props: list[Proposal] = []
    for field in ("artist", "title"):
        value = track.get(field)
        if not value:
            continue
        new = fold_fullwidth(value)
        new = _SPACED_CENSOR.sub(lambda m: m.group().replace(" ", ""), new)
        new = _MULTISPACE.sub(" ", new).strip()
        if new != value:
            props.append(_p(track, field, value, new, Confidence.HIGH, "normalize", 100, "canonical ascii/spacing"))
    return props


# --- registry ---------------------------------------------------------------


def _p(
    track: Track,
    field: str,
    current: str,
    proposed: str,
    conf: Confidence,
    fixer_name: str,
    score: int,
    reason: str,
) -> Proposal:
    return make_proposal(track, field, current, proposed, conf, fixer_name, score, reason)


# Derived from the registry, ordered by priority (single source of truth).
_ORDERED = sorted(REGISTRY.values(), key=lambda r: r.priority)
FIXERS: dict[str, Fixer] = {r.name: r.fn for r in _ORDERED}


def priority(name: str) -> int:
    """Dedup/run priority for a fixer name (lower wins ties); 99 if unknown."""
    reg = REGISTRY.get(name)
    return reg.priority if reg else 99
