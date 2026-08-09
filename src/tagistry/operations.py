"""One operation per user-facing action: build providers, drive the pipeline, return plain facts.

This is the single orchestration layer the CLI and the MCP server both call. Each function owns
the decision "which providers + which pipeline calls + what result data" ONCE, so the two adapters
can't drift (the MCP tool and the CLI command used to be two hand-kept copies of the same wiring).
The adapters stay thin: the CLI renders these dicts to the terminal, the MCP server returns them as
JSON. Warnings are returned, never printed, so each adapter surfaces them its own way.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import audits, config, covers, pipeline, planners
from .assembly import build_providers
from .domain import ApplyResult, ProgressFn, Proposal


def _fixer_names(fixers: str | None) -> list[str] | None:
    """A comma-separated --fixers value into a name list (None = all fixers)."""
    return [f.strip() for f in fixers.split(",")] if fixers else None


def _by_fixer(proposals: list[Proposal]) -> list[dict[str, object]]:
    counts = Counter((p.fixer, p.confidence.value) for p in proposals)
    return [{"fixer": f, "confidence": c, "count": n} for (f, c), n in sorted(counts.items())]


def _result(r: ApplyResult) -> dict[str, object]:
    return {"applied": r.applied, "skipped": r.skipped, "locked": r.locked, "errors": r.errors}


def scan(
    root: str,
    *,
    review: str,
    fixers: str | None = None,
    online: bool = True,
    fingerprint: bool = False,
    researcher: str = "none",
    discogs: bool = False,
    resume: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, object]:
    """Scan a library and stage proposals to a review CSV. With resume, append to the CSV and skip
    files already in it. Returns the staged count, the per-(fixer, confidence) breakdown, warnings."""
    providers, warnings = build_providers(online, fingerprint, researcher, discogs=discogs)
    names = _fixer_names(fixers)
    checkpoint = review if resume else None
    proposals = pipeline.scan(root, providers, names, progress=progress, checkpoint=checkpoint)
    if resume:
        total = len(pipeline.read_review(review))
        staged = total
    else:
        staged = pipeline.write_review(proposals, review)
    return {
        "staged": staged,
        "new": len(proposals),
        "review": review,
        "resumed": resume,
        "by_fixer": _by_fixer(proposals),
        "warnings": warnings,
    }


def list_proposals(review: str) -> list[dict[str, str]]:
    """The staged proposals from a review CSV, as plain rows (an agent-facing raw dump)."""
    return [r.to_dict() for r in pipeline.read_review(review)]


def review_summary(review: str) -> dict[str, object]:
    """Counts per (fixer, confidence, decision) for a staged review CSV."""
    rows = pipeline.read_review(review)
    counts = Counter((r.fixer, r.confidence, r.apply) for r in rows)
    breakdown = [{"fixer": f, "confidence": c, "decision": d, "count": n} for (f, c, d), n in sorted(counts.items())]
    return {"total": len(rows), "review": review, "breakdown": breakdown}


def apply(review: str, log: str, dry_run: bool = False) -> dict[str, object]:
    """Apply kept rows from the review CSV, appending to the undo log."""
    return {**_result(pipeline.apply(review, log, dry_run=dry_run)), "dry_run": dry_run}


def undo(
    n: int,
    log: str,
    *,
    ids: list[str] | None = None,
    run: str = "",
    path: str = "",
    dry_run: bool = False,
) -> dict[str, object]:
    """Reverse selected changes: the ones named by `ids`, or a whole `run`, or every logged change
    whose file matches the `path` glob -- else the last n. dry_run reports the selection only."""
    r = pipeline.undo(log, n, ids=ids or (), run=run, path=path, dry_run=dry_run)
    return {"reverted": r.applied, "changes": r.reverted, "locked": r.locked, "errors": r.errors, "dry_run": dry_run}


def list_changes(log: str, limit: int = 0) -> list[dict[str, str]]:
    """Every undo-able change, newest first, each with the id `undo --id` takes."""
    return pipeline.list_changes(log, limit)


def status(log: str) -> dict[str, object]:
    """How many changes are logged and the most recent one."""
    return pipeline.status(log)


def rename(
    root: str,
    *,
    all_folders: bool = False,
    stage: str | None = None,
    dry_run: bool = False,
    log: str,
    progress: ProgressFn | None = None,
) -> dict[str, object]:
    """Rename files to 'artist - title'. With stage, write a plan CSV and touch nothing; else apply."""
    plan = pipeline.rename_plan(pipeline.read_tracks(root, progress), rename_all=all_folders)
    if stage:
        return {"staged": pipeline.write_rename_plan(plan, stage), "plan": stage}
    r = pipeline.apply_renames(plan, log, dry_run=dry_run)
    return {"renamed": r.applied, "skipped": r.skipped, "locked": r.locked, "errors": r.errors, "dry_run": dry_run}


def apply_renames(plan: str, *, dry_run: bool = False, log: str) -> dict[str, object]:
    """Apply a reviewed rename plan CSV."""
    pairs = pipeline.read_rename_plan(plan)
    r = pipeline.apply_renames(pairs, log, dry_run=dry_run)
    return {"renamed": r.applied, "skipped": r.skipped, "locked": r.locked, "errors": r.errors, "dry_run": dry_run}


def coverart(
    root: str, *, mode: str = "folder", replace: bool = False, dry_run: bool = False, log: str
) -> dict[str, object]:
    """Fetch cover art: mode='folder' writes one cover.jpg per album folder, mode='embed' embeds
    into each art-less file. An unknown mode returns an {error}."""
    from .providers.coverart import default_fetcher

    fetcher = default_fetcher(config.cache_path("coverart_cache"))
    if mode == "embed":
        r = covers.embed_covers(root, fetcher, log, replace=replace, dry_run=dry_run)
    elif mode == "folder":
        r = covers.save_folder_covers(root, fetcher, log, dry_run=dry_run)
    else:
        return {"error": "mode must be 'folder' or 'embed'"}
    return {
        "written": r.applied,
        "skipped": r.skipped,
        "locked": r.locked,
        "errors": r.errors,
        "dry_run": dry_run,
        "mode": mode,
    }


def disambiguate(
    review: str,
    *,
    out: str | None = None,
    online: bool = True,
    researcher: str = "cli",
    timeout: int = 120,
    progress: ProgressFn | None = None,
) -> dict[str, object]:
    """Ask a layer-2 agent to confirm/correct the REVIEW rows in a staged CSV."""
    providers, warnings = build_providers(online, fingerprint=False, researcher=researcher, researcher_timeout=timeout)
    touched = pipeline.disambiguate(review, providers, out, progress=progress)
    return {"touched": touched, "review": out or review, "warnings": warnings}


def adjudicate(review: str, out: str | None = None) -> dict[str, object]:
    """Decide REVIEW rows by the deterministic policies."""
    return {**pipeline.adjudicate(review, out), "review": out or review}


def markers(review: str, out: str) -> dict[str, object]:
    """Filter a fingerprint scan CSV to titles that RESTORE a stripped version marker."""
    return {"staged": pipeline.markers(review, out), "out": out}


def shazam_filter(review: str, verdicts: str, out: str | None = None) -> dict[str, object]:
    """Downgrade every fingerprint proposal a second fingerprinter (Shazam) did not AGREE with."""
    return {**pipeline.shazam_filter(review, verdicts, out), "review": out or review}


def scrobble_check(review: str, out: str | None = None) -> dict[str, object]:
    """Final gate: downgrade any title/artist change last.fm does NOT know (would orphan scrobbles)."""
    providers, warnings = build_providers(online=False, fingerprint=False, lastfm=True)
    counts = pipeline.check_scrobble_coverage(review, providers, out)
    return {**counts, "review": out or review, "warnings": warnings}


def scrobble_names(root: str, review: str, progress: ProgressFn | None = None) -> dict[str, object]:
    """Retag each artist to its most-scrobbled last.fm spelling (via MusicBrainz aliases), staged."""
    providers, warnings = build_providers(online=True, fingerprint=False, lastfm=True)
    proposals = planners.plan_scrobble_names(pipeline.read_tracks(root, progress), providers)
    n = pipeline.write_review(proposals, review)
    return {"staged": n, "review": review, "warnings": warnings}


def albumartist(root: str, review: str, progress: ProgressFn | None = None) -> dict[str, object]:
    """Fill a blank albumartist from the folder's dominant artist (single-artist albums), staged."""
    proposals = planners.plan_albumartist(pipeline.read_tracks(root, progress))
    n = pipeline.write_review(proposals, review)
    return {"staged": n, "review": review}


def duplicates(root: str, limit: int = 50, progress: ProgressFn | None = None) -> dict[str, object]:
    """Files that share an artist+title (metadata duplicates), best-quality first."""
    groups = audits.find_duplicates(root, progress)
    sample = [
        {"artist": g[0].get("artist"), "title": g[0].get("title"), "count": len(g), "keep": Path(g[0].path).name}
        for g in groups[:limit]
    ]
    return {"groups": len(groups), "sample": sample}


def doctor(root: str, progress: ProgressFn | None = None) -> dict[str, object]:
    """Tag anomalies (blank fields, artist==title, multi-artist), counted by type (most common first)."""
    issues = audits.audit_library(root, progress)
    return {"issues": len(issues), "by_type": dict(Counter(msg for _p, msg in issues).most_common())}


def clean(root: str, dry_run: bool = False) -> dict[str, object]:
    """Delete orphaned atomic-write temp files ('*.tagistry.*.tmp')."""
    deleted, locked = pipeline.clean_temp_files(root, dry_run=dry_run)
    return {"deleted": deleted, "locked": locked}


def plex_refresh(url: str | None = None, token: str | None = None) -> dict[str, object]:
    """Tell Plex to rescan its music sections. Reads PLEX_URL/PLEX_TOKEN when not passed; an unset
    credential or a non-http(s) URL (SSRF floor) returns an {error} instead of raising."""
    import os
    from urllib.parse import urlparse

    from .providers.plex import default_plex

    base, tok = url or os.environ.get("PLEX_URL"), token or os.environ.get("PLEX_TOKEN")
    if not base or not tok:
        return {"error": "set PLEX_URL and PLEX_TOKEN (or pass url/token)"}
    if urlparse(base).scheme not in ("http", "https"):  # reject file://, etc. (SSRF floor)
        return {"error": "PLEX_URL must be an http(s) URL"}
    return {"refreshed": default_plex(base, tok).refresh_music()}


def direction_digest(review: str) -> str:
    """The apply-decision digest (one line per apply row) for an adapter to display."""
    return pipeline.direction_digest(review)
