"""The gates: decide the staged review rows before apply.

Each gate reads the review CSV, re-decides the open REVIEW rows by one arbiter -- the deterministic
review policies (adjudicate), a layer-2 agent (disambiguate), a second fingerprinter (shazam_filter),
or last.fm scrobble coverage (check_scrobble_coverage) -- and rewrites the CSV in place. markers /
direction_digest are the read-only views over it. This is distinct from scan (produce rows) and
apply (write to disk); the review-store owns the file format, this owns the decisions.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from dataclasses import replace

from . import shazam, tagio
from .domain import Confidence, ProgressFn, ReviewRow
from .fixers import FIXERS
from .policies import VERDICTS, adjudicate_change
from .providers import LastFm, Providers
from .research import ResearchQuestion
from .reviewstore import read_review, write_review_rows
from .text import adds_version_marker, key, tokens

logger = logging.getLogger(__name__)

# A retitle from ONE fingerprinter is untrusted until a second, independent one agrees
FINGERPRINT_FIXERS = {"blank_id", "canonicalize"}
# Pinned at import: a @fixer rename would otherwise leave this stale and the gate failing OPEN
if _unknown := FINGERPRINT_FIXERS - set(FIXERS):
    raise RuntimeError(f"FINGERPRINT_FIXERS names an unknown fixer: {_unknown}")


def _needs_research(row: ReviewRow) -> bool:
    """A REVIEW row the deterministic layer left open, not a fingerprint retitle, not already kept."""
    return row.confidence == Confidence.REVIEW.value and not row.is_apply and row.fixer != "canonicalize"


def disambiguate(
    review_csv: str, providers: Providers, out_csv: str | None = None, progress: ProgressFn | None = None
) -> int:
    """Ask the layer-2 researcher to verify each REVIEW row the deterministic layer left open.

    A confirmed proposal flips to 'apply' ONLY when the deterministic policy also passes it
    (policies.adjudicate_change == 'apply'); a policy-flagged rewrite the agent confirmed stays
    'skip' with the reason recorded, so a confident agent can't route a context-drop / field-clear
    / accent-strip around the policy. A corrected proposal updates 'proposed' but stays 'skip' for a
    human to see. Fingerprint (`canonicalize`) rows are left untouched -- they belong to the
    two-fingerprinter shazam-filter + adjudicate flow, not a single agent confirmation. HIGH and
    already-kept rows are never touched. `progress(done, total, path)` is called per researched row
    (each is a slow agent call). Returns how many rows changed. Writes to out_csv, else in place.
    """
    researcher = providers.researcher
    rows = read_review(review_csv)
    total = sum(1 for r in rows if _needs_research(r))
    out: list[ReviewRow] = []
    touched = worked = 0
    for row in rows:
        if not _needs_research(row):
            out.append(row)
            continue
        worked += 1
        if progress is not None:
            with contextlib.suppress(Exception):
                progress(worked, total, row.path)  # cosmetic: a report error must not abort the pass
        q = ResearchQuestion(
            kind="verify_proposal",
            ask=(
                f"Should the {row.field} of this track change from '{row.current}' to "
                f"'{row.proposed}'? Set decision 'confirm' to accept the proposed value, or return the "
                f"correct value. Prefer ASCII. Cite a source or answer uncertain."
            ),
            context={"field": row.field, "current": row.current, "proposed": row.proposed},
        )
        ans = researcher.resolve(q)
        if not ans.is_usable:
            out.append(row)
            continue
        corrected = ans.value and key(ans.value) not in (key(row.proposed), key(row.current))
        if corrected and ans.value:
            # A correction stays 'skip', but record how the policy reads the NEW value
            verdict, reason = adjudicate_change(row.current, ans.value)
            note = "" if verdict == "apply" else f" (policy {verdict}: {reason})"
            row = replace(row, proposed=ans.value, evidence=f"researcher corrected: {ans.reasoning[:60]}{note}")
        else:
            # The agent confirms WITHIN the policy; it cannot override a flagged rewrite
            verdict, reason = adjudicate_change(row.current, row.proposed)
            if verdict == "apply":
                row = replace(row, apply="apply", evidence=f"researcher confirmed: {ans.reasoning[:70]}")
            else:
                row = replace(row, evidence=f"researcher confirmed BUT policy {verdict}: {reason}")
        touched += 1
        out.append(row)
    write_review_rows(out, out_csv or review_csv)
    return touched


def adjudicate(review_csv: str, out_csv: str | None = None) -> dict[str, int]:
    """Decide each REVIEW row by the deterministic policies (policies.adjudicate_change):
    'apply' flips it to apply, 'flag'/'reject' stay 'skip' with the reason recorded. This is the
    code version of the agent review pass -- reproducible and golden-tested, no prompt.
    HIGH and already-kept rows are untouched. Returns the count per verdict."""
    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    out: list[ReviewRow] = []
    for row in read_review(review_csv):
        if row.confidence != Confidence.REVIEW.value or row.is_apply:
            out.append(row)
            continue
        verdict, reason = adjudicate_change(row.current, row.proposed)
        counts[verdict] += 1
        decision = "apply" if verdict == "apply" else row.apply
        out.append(replace(row, apply=decision, evidence=f"policy {verdict}: {reason}"))
    write_review_rows(out, out_csv or review_csv)
    return counts


def select_marker_restores(rows: Iterable[ReviewRow]) -> list[ReviewRow]:
    """Keep the fingerprint rows whose proposed TITLE restores a stripped version marker.

    The canonicalize fixer proposes the MusicBrainz canonical title for the fingerprinted audio,
    which is noisy (every tag/canonical diff). This narrows it to the rows the user cares about:
    a title that gains a remix/live/edit/... marker the current tag had lost. Title field only --
    markers live in the title, not the artist. A blank current title has no marker to restore, so
    it is excluded (that is a blank-title fill, not a marker restore)."""
    return [
        r
        for r in rows
        if r.fixer == "canonicalize"
        and r.field == "title"
        and r.current.strip()
        and adds_version_marker(r.current, r.proposed)
    ]


def markers(review_csv: str, out_csv: str) -> int:
    """Read a scan CSV, write only the version-marker restore rows to out_csv for per-item review."""
    hits = select_marker_restores(read_review(review_csv))
    write_review_rows(hits, out_csv)
    return len(hits)


def _digest_line(row: ReviewRow) -> str:
    decision = "apply" if row.is_apply else "skip"
    tag = " [reversible]" if row.is_apply else ""  # every applied change is undo-able
    ident = f"{row.file_artist} - {row.file_title} | " if (row.file_artist or row.file_title) else ""
    return (
        f"[{decision}] {ident}{row.fixer} {row.field}: '{row.current}' -> '{row.proposed}' ({row.evidence[:60]}){tag}"
    )


def direction_digest(review_csv: str, only_apply: bool = True) -> str:
    """One line per decision, so an autonomous run surfaces WHAT it chose to stdout without opening
    the CSV -- you scan decisions, not diffs. Defaults to the apply rows (the consequential
    ones); pass only_apply=False for every row. Empty string when there is nothing to show."""
    rows = read_review(review_csv)
    if only_apply:
        rows = [r for r in rows if r.is_apply]
    return "\n".join(_digest_line(r) for r in rows)


def shazam_filter(review_csv: str, verdicts_csv: str, out_csv: str | None = None) -> dict[str, int]:
    """Downgrade every fingerprint proposal a second fingerprinter did not AGREE with.

    The verdicts CSV (from scripts/shazam_verify.py) maps a file path to Shazam's verdict on the
    proposed title AND the artist Shazam heard. A fingerprint retitle is trustworthy ONLY when both
    fingerprinters agree, so a fingerprint row whose path has no AGREE verdict is forced to skip (and
    HIGH -> REVIEW) with the reason recorded. The AGREE verdict compares TITLES, so a row that
    rewrites the ARTIST needs Shazam's artist to match too -- else a wrong-upload artist (AcoustID
    matched a cover/remix) would pass on a bare title agreement. Non-fingerprint rows pass through
    untouched. Rows are matched by path. Returns {agree, downgraded, untouched}."""
    verdicts = shazam.read_verdicts(verdicts_csv)
    counts = {"agree": 0, "downgraded": 0, "untouched": 0}
    out: list[ReviewRow] = []
    for row in read_review(review_csv):
        if row.fixer not in FINGERPRINT_FIXERS:
            counts["untouched"] += 1
            out.append(row)
            continue
        v = verdicts.get(row.path)
        if v is None or v.verdict != shazam.AGREE:
            confirmed, reason = (
                False,
                f"shazam {(v.verdict if v else '') or 'no verdict'}: 2nd fingerprinter did not confirm",
            )
        elif row.field == "artist" and not _artist_corroborated(row.proposed, v.shazam_artist):
            confirmed, reason = False, f"shazam heard artist '{v.shazam_artist}': does not confirm the artist rewrite"
        else:
            confirmed, reason = True, ""
        if confirmed:
            counts["agree"] += 1
            out.append(row)
            continue
        counts["downgraded"] += 1
        conf = Confidence.REVIEW.value if row.confidence == Confidence.HIGH.value else row.confidence
        out.append(replace(row, apply="skip", confidence=conf, evidence=reason))
    write_review_rows(out, out_csv or review_csv)
    return counts


def _artist_corroborated(proposed: str, shazam_artist: str) -> bool:
    """The proposed artist is confirmed by the artist Shazam heard (accent/&/case-insensitive, and
    token-subset either way so 'Sia' matches 'Sia & Sean Paul'). Cross-script (a Hebrew rewrite vs a
    Latin Shazam name) will not match, so that rewrite is left for a human -- the safe default for an
    unverifiable identity change.

    Token SETS, not substrings: 'Sia' is a substring of 'Siames' and 'Nas' of 'Nasty C', so
    containment on space-stripped text corroborates unrelated acts.
    """
    p, s = tokens(proposed), tokens(shazam_artist)
    return bool(p) and bool(s) and (p <= s or s <= p)


def check_scrobble_coverage(review_csv: str, providers: Providers, out_csv: str | None = None) -> dict[str, int]:
    """Final gate on the correctness arbiter: for each file with an apply row that changes its
    title/artist, ask last.fm whether it KNOWS the resulting (artist, title). A retitle MusicBrainz
    endorses but last.fm doesn't know orphans every future scrobble, so a not-known result
    downgrades that file's title/artist apply rows to skip. Needs the KEYED LastFm API
    (track.getInfo) -- a no-op with the keyless page scraper or offline (build_providers WARNs).
    Run post-stage, not at scan time: one network call per changed file. Returns {checked, downgraded}."""
    lf = providers.lastfm
    counts = {"checked": 0, "downgraded": 0}
    if not isinstance(lf, LastFm):
        return counts  # the track-level gate needs the API key; silently off with the page scraper
    rows = read_review(review_csv)
    changed: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.is_apply and row.field in ("artist", "title"):
            changed.setdefault(row.path, {})[row.field] = row.proposed
    orphaned: set[str] = set()
    for path, fields in changed.items():
        try:
            track = tagio.read(path)
        except Exception as exc:
            logger.debug("scrobble check: unreadable %s: %s", path, exc)
            continue  # unreadable -> can't verify, leave the rows alone
        artist = fields.get("artist") or track.get("artist")
        title = fields.get("title") or track.get("title")
        if not artist or not title:
            continue
        counts["checked"] += 1
        if not lf.track_exists(artist, title):
            orphaned.add(path)
    if not orphaned:
        return counts
    ev = "last.fm does not know this artist/title -- would orphan scrobbles"
    out = [
        replace(row, apply="skip", evidence=ev)
        if (row.is_apply and row.field in ("artist", "title") and row.path in orphaned)
        else row
        for row in rows
    ]
    # What THIS run flipped, else a second pass re-counts rows a prior one already downgraded
    counts["downgraded"] = sum(1 for old, new in zip(rows, out, strict=True) if old.apply != new.apply)
    write_review_rows(out, out_csv or review_csv)
    return counts
