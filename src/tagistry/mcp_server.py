"""Thin fastmcp adapter so an agent can drive Tagistry. Each tool is a passthrough to
operations.py (the one orchestration layer the CLI also uses), so the two can't drift.
"""

from __future__ import annotations

from . import operations
from .pipeline import DEFAULT_LOG, DEFAULT_MARKERS, DEFAULT_REVIEW, DEFAULT_VERDICTS

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit('the MCP extra is not installed; run: uv tool install "tagistry[mcp]"') from exc

mcp: FastMCP = FastMCP("tagistry")


@mcp.tool
def scan(
    root: str,
    review: str = DEFAULT_REVIEW,
    online: bool = True,
    fingerprint: bool = False,
    researcher: str = "none",
    fixers: str | None = None,
    discogs: bool = False,
    resume: bool = False,
) -> dict[str, object]:
    """Scan a library and stage proposals to a review CSV. `fixers` is a comma-separated subset;
    `resume` appends to the CSV and skips files already in it; `discogs` fills blank genres."""
    return operations.scan(
        root,
        review=review,
        fixers=fixers,
        online=online,
        fingerprint=fingerprint,
        researcher=researcher,
        discogs=discogs,
        resume=resume,
    )


@mcp.tool
def list_proposals(review: str = DEFAULT_REVIEW) -> list[dict[str, str]]:
    """Return the staged proposals from a review CSV."""
    return operations.list_proposals(review)


@mcp.tool
def apply(review: str = DEFAULT_REVIEW, log: str = DEFAULT_LOG, dry_run: bool = True) -> dict[str, object]:
    """Apply kept rows from the review CSV, appending to the undo log. Defaults to dry_run so an
    agent never writes tags unreviewed; pass dry_run=False to write."""
    return operations.apply(review, log, dry_run=dry_run)


@mcp.tool
def undo(
    n: int = 1,
    log: str = DEFAULT_LOG,
    ids: list[str] | None = None,
    run: str = "",
    path: str = "",
    dry_run: bool = False,
) -> dict[str, object]:
    """Reverse applied changes: the change ids in `ids`, or every change of one `run`, or every
    change whose file matches the `path` glob -- else the last n. Selectors are OR-ed. Ids and runs
    come from list_changes; dry_run returns the selection without reverting anything."""
    return operations.undo(n, log, ids=ids, run=run, path=path, dry_run=dry_run)


@mcp.tool
def list_changes(log: str = DEFAULT_LOG, limit: int = 0) -> list[dict[str, str]]:
    """List the undo-able changes, newest first, each with the id and run id undo selects on."""
    return operations.list_changes(log, limit)


@mcp.tool
def status(log: str = DEFAULT_LOG) -> dict[str, object]:
    """Report how many changes are logged and the most recent one."""
    return operations.status(log)


@mcp.tool
def rename(
    root: str,
    all_folders: bool = False,
    stage: str | None = None,
    dry_run: bool = True,
    log: str = DEFAULT_LOG,
) -> dict[str, object]:
    """Rename files to 'artist - title' (filesystem-safe). Skips album folders unless all_folders.
    Defaults to dry_run so an agent never moves files unreviewed; pass a `stage` path to write a
    reviewable plan CSV, or dry_run=False to actually rename."""
    return operations.rename(root, all_folders=all_folders, stage=stage, dry_run=dry_run, log=log)


@mcp.tool
def coverart(
    root: str, mode: str = "folder", replace: bool = False, dry_run: bool = True, log: str = DEFAULT_LOG
) -> dict[str, object]:
    """Fetch cover art (Cover Art Archive -> iTunes). mode='folder' writes one cover.jpg per album
    folder (cheap); mode='embed' embeds into each art-less file. Defaults to dry_run so an agent
    never writes art unreviewed; pass dry_run=False to write."""
    return operations.coverart(root, mode=mode, replace=replace, dry_run=dry_run, log=log)


@mcp.tool
def disambiguate(
    review: str = DEFAULT_REVIEW,
    out: str | None = None,
    online: bool = True,
    researcher: str = "cli",
    timeout: int = 120,
) -> dict[str, object]:
    """Ask a layer-2 agent to confirm/correct the REVIEW rows in a staged CSV."""
    return operations.disambiguate(review, out=out, online=online, researcher=researcher, timeout=timeout)


@mcp.tool
def adjudicate(review: str = DEFAULT_REVIEW, out: str | None = None) -> dict[str, object]:
    """Decide REVIEW rows by the deterministic policies (keep accents, flag context-drops and
    version markers, apply verified co-leads). 'apply' rows flip to apply; the rest stay skipped."""
    return operations.adjudicate(review, out)


@mcp.tool
def markers(review: str = DEFAULT_REVIEW, out: str = DEFAULT_MARKERS) -> dict[str, object]:
    """Filter a --fingerprint scan CSV to titles that RESTORE a stripped version marker, for review."""
    return operations.markers(review, out)


@mcp.tool
def shazam_filter(
    review: str = DEFAULT_REVIEW, verdicts: str = DEFAULT_VERDICTS, out: str | None = None
) -> dict[str, object]:
    """Downgrade every fingerprint proposal a 2nd fingerprinter (Shazam) did not AGREE with. Run
    scripts/shazam_verify.py first to produce the verdicts CSV."""
    return operations.shazam_filter(review, verdicts, out)


@mcp.tool
def scrobble_check(review: str = DEFAULT_REVIEW, out: str | None = None) -> dict[str, object]:
    """Final gate: downgrade any title/artist change last.fm does NOT know (would orphan scrobbles).
    Needs a last.fm API key; a no-op without one."""
    return operations.scrobble_check(review, out)


@mcp.tool
def scrobble_names(root: str, review: str = DEFAULT_REVIEW) -> dict[str, object]:
    """Retag each artist to its most-scrobbled last.fm spelling (via MusicBrainz aliases), staged."""
    return operations.scrobble_names(root, review)


@mcp.tool
def albumartist(root: str, review: str = DEFAULT_REVIEW) -> dict[str, object]:
    """Fill a blank albumartist from the folder's dominant artist (single-artist albums), staged."""
    return operations.albumartist(root, review)


@mcp.tool
def duplicates(root: str, limit: int = 50) -> dict[str, object]:
    """Report files that share an artist+title (metadata duplicates), best-quality first."""
    return operations.duplicates(root, limit)


@mcp.tool
def doctor(root: str) -> dict[str, object]:
    """Report tag anomalies (blank fields, artist==title, multi-artist) without changing anything."""
    return operations.doctor(root)


@mcp.tool
def review(review: str = DEFAULT_REVIEW) -> dict[str, object]:
    """Summarize a staged review CSV: counts per (fixer, confidence, decision)."""
    return operations.review_summary(review)


@mcp.tool
def apply_renames(plan: str, dry_run: bool = True, log: str = DEFAULT_LOG) -> dict[str, object]:
    """Apply a reviewed rename plan CSV (from rename with stage). Defaults to dry_run."""
    return operations.apply_renames(plan, dry_run=dry_run, log=log)


@mcp.tool
def clean(root: str, dry_run: bool = True) -> dict[str, object]:
    """Delete orphaned atomic-write temp files ('*.tagistry.*.tmp') left when a write's cleanup was
    blocked by a lock. Defaults to dry_run so an agent never unlinks unreviewed."""
    return operations.clean(root, dry_run=dry_run)


@mcp.tool
def plex_refresh(url: str | None = None, token: str | None = None) -> dict[str, object]:
    """Tell Plex to rescan its music sections so it picks up the tag/art edits (post-apply sink)."""
    return operations.plex_refresh(url, token)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
