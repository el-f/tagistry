"""Thin typer CLI over operations.py (the shared orchestration layer). Renders facts to the
terminal; the MCP server returns the same facts as JSON. No pipeline wiring lives here."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__, config, operations
from .pipeline import DEFAULT_LOG, DEFAULT_MARKERS, DEFAULT_REVIEW, DEFAULT_VERDICTS

app = typer.Typer(add_completion=False, help="Tagistry: a music tag-correction engine.")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"tagistry {__version__}")
        raise typer.Exit


@app.callback()
def _configure(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging (per-file skips, fixer errors).")
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    """Set up logging once for every command. WARNING by default (a skipped/corrupt file shows);
    --verbose or $TAGISTRY_LOG_LEVEL=DEBUG surfaces per-file fixer errors."""
    import logging
    import os

    level_name = os.environ.get("TAGISTRY_LOG_LEVEL", "").upper() or ("DEBUG" if verbose else "WARNING")
    logging.basicConfig(
        level=getattr(logging, level_name, logging.WARNING), format="%(levelname)s %(name)s: %(message)s"
    )


def _log_header(log: str) -> None:
    """Show where the change log (the undo net) resolves, so a write is never invisible. On stderr:
    it is context, not output, and `changes`/`undo` pipe their rows into grep and awk."""
    typer.secho(f"log: {log}", fg=typer.colors.BLUE, err=True)


def _warn(warnings: object) -> None:
    """Render provider warnings (missing key, offline fallbacks) in yellow."""
    for w in warnings if isinstance(warnings, list) else []:
        typer.secho(str(w), fg=typer.colors.YELLOW)


def _print_direction(review: str) -> None:
    """Surface the apply DECISIONS (one line each) so an autonomous run is scannable at a glance."""
    digest = operations.direction_digest(review)
    if digest:
        typer.secho("DIRECTION (apply decisions):", fg=typer.colors.CYAN)
        typer.echo(digest)


def _progress() -> Callable[[int, int, str], None]:
    """A per-file tqdm progress bar as an injected callback (count + rate + ETA -- what a
    multi-minute big-library read needs). The core stays display-free; only the CLI shows a bar.
    `disable=None` shows it in a terminal but stays silent in a pipe/redirect/test (also dodges a
    cp1252 Windows crash on redirect). Lazily created on the first call, closed on the last."""
    from tqdm import tqdm

    bar: tqdm[Any] | None = None

    def report(done: int, total: int, path: str) -> None:
        nonlocal bar
        if bar is None:
            bar = tqdm(total=total, unit="file", leave=False, disable=None)
        bar.update(done - bar.n)
        # ascii-fold the name: a cp1252 console can crash on an accented/Hebrew filename
        bar.set_postfix_str(Path(path).name[:40].encode("ascii", "replace").decode("ascii"), refresh=False)
        if done >= total:
            bar.close()

    return report


def _resolve_root(root: str | None) -> str:
    """The library root: the CLI argument, else $TAGISTRY_ROOT / config.toml. A command needs a
    root, so a missing or non-existent one is a clear error, not a silent empty scan."""
    resolved = root or config.default_root()
    if not resolved:
        raise typer.BadParameter("no library root given and TAGISTRY_ROOT is not set")
    # A quoted "~/Music" reaches us unexpanded -- without this the scan finds nothing and says so.
    path = Path(resolved).expanduser()
    if not path.is_dir():
        raise typer.BadParameter(f"library root does not exist: {path}")
    return str(path)


def _change_line(row: dict[str, str]) -> str:
    """One logged change on one line: the id and run that address it, then what it did."""
    label = row["field"] if row["kind"] == "tag" else row["kind"]
    return (
        f"{row['id']}  {row['time']}  run {row['run'] or '-'}  "
        f"{Path(row['path']).name}  {label}: {row['old']!r} -> {row['new']!r}"
    )


def _report_renames(result: dict[str, object], dry_run: bool) -> None:
    """Shared render for the two rename ops: a green summary, then the locked list and any errors."""
    tag = "would rename" if dry_run else "renamed"
    typer.secho(f"{tag} {result['renamed']} files, skipped {result['skipped']}", fg=typer.colors.GREEN)
    locked = result["locked"]
    if isinstance(locked, list) and locked:
        typer.secho(f"locked ({len(locked)}): {', '.join(locked[:5])}", fg=typer.colors.YELLOW)
    for err in result["errors"] if isinstance(result["errors"], list) else []:
        typer.secho(str(err), fg=typer.colors.RED)


@app.command()
def scan(
    root: Annotated[str | None, typer.Argument(help="Library root to scan (or $TAGISTRY_ROOT).")] = None,
    review: Annotated[str, typer.Option(help="Review CSV to write.")] = DEFAULT_REVIEW,
    fixers: Annotated[str | None, typer.Option(help="Comma-separated fixer subset.")] = None,
    online: Annotated[bool, typer.Option(help="Use MusicBrainz to verify.")] = True,
    fingerprint: Annotated[
        bool,
        typer.Option(
            help="Fingerprint audio (AcoustID): blank_id + canonicalize + year_fill. Gate with shazam-filter."
        ),
    ] = False,
    researcher: Annotated[
        str, typer.Option(help="Layer-2 agent for hard residue: 'cli', 'http', 'majority', or 'none'.")
    ] = "none",
    discogs: Annotated[bool, typer.Option(help="Fill blank genres from Discogs ($DISCOGS_TOKEN).")] = False,
    resume: Annotated[
        bool, typer.Option(help="Resume a killed scan: append to the review CSV, skip files already in it.")
    ] = False,
) -> None:
    """Scan a library and stage proposals to a review CSV."""
    result = operations.scan(
        _resolve_root(root),
        review=review,
        fixers=fixers,
        online=online,
        fingerprint=fingerprint,
        researcher=researcher,
        discogs=discogs,
        resume=resume,
        progress=_progress(),
    )
    _warn(result["warnings"])
    if resume:
        typer.secho(
            f"staged {result['staged']} proposals in {review} (+{result['new']} new this run)", fg=typer.colors.GREEN
        )
        return
    typer.secho(f"staged {result['staged']} proposals -> {review}", fg=typer.colors.GREEN)
    for row in result["by_fixer"] if isinstance(result["by_fixer"], list) else []:
        typer.echo(f"  {row['fixer']:14} {row['confidence']:6} {row['count']}")


@app.command()
def review(path: Annotated[str, typer.Option("--review", help="Review CSV.")] = DEFAULT_REVIEW) -> None:
    """Summarize a staged review CSV."""
    result = operations.review_summary(path)
    typer.echo(f"{result['total']} proposals in {path}")
    for row in result["breakdown"] if isinstance(result["breakdown"], list) else []:
        typer.echo(f"  {row['fixer']:14} {row['confidence']:6} {row['decision']:5} {row['count']}")


@app.command()
def disambiguate(
    review: Annotated[str, typer.Option(help="Review CSV to resolve REVIEW rows in.")] = DEFAULT_REVIEW,
    out: Annotated[str | None, typer.Option(help="Write result here instead of in place.")] = None,
    online: Annotated[bool, typer.Option(help="Cross-verify the agent against MusicBrainz.")] = True,
    researcher: Annotated[str, typer.Option(help="Layer-2 agent: 'cli', 'http', 'majority', or 'none'.")] = "cli",
    timeout: Annotated[int, typer.Option(help="Per-question agent timeout, seconds.")] = 120,
) -> None:
    """Ask a layer-2 agent to confirm or correct the REVIEW rows in a staged CSV."""
    result = operations.disambiguate(
        review, out=out, online=online, researcher=researcher, timeout=timeout, progress=_progress()
    )
    _warn(result["warnings"])
    typer.secho(f"researcher touched {result['touched']} rows in {result['review']}", fg=typer.colors.GREEN)
    _print_direction(out or review)


@app.command()
def adjudicate(
    review: Annotated[str, typer.Option(help="Review CSV to adjudicate REVIEW rows in.")] = DEFAULT_REVIEW,
    out: Annotated[str | None, typer.Option(help="Write result here instead of in place.")] = None,
) -> None:
    """Decide REVIEW rows by the deterministic policies (keep accents, flag context-drops
    and version markers, apply verified co-leads). Reproducible, no agent -- the code version of
    a human review pass. 'apply' rows flip to apply; the rest stay skipped with a reason."""
    result = operations.adjudicate(review, out)
    typer.secho(
        f"adjudicated: {result['apply']} apply, {result['flag']} flag, {result['reject']} reject -> {result['review']}",
        fg=typer.colors.GREEN,
    )
    _print_direction(out or review)


@app.command()
def markers(
    review: Annotated[str, typer.Option(help="Scan CSV to filter (from a --fingerprint scan).")] = DEFAULT_REVIEW,
    out: Annotated[str, typer.Option(help="Where to write the marker-restore rows.")] = DEFAULT_MARKERS,
) -> None:
    """Filter a fingerprint scan to titles that RESTORE a stripped version marker (remix/live/
    edit/...). The audio, via AcoustID+MusicBrainz, is the strict source; these are staged for
    per-item review, never auto-applied. Run `scan --fingerprint` first, then shazam-filter (2nd
    fingerprinter) -> adjudicate/apply."""
    result = operations.markers(review, out)
    typer.secho(f"staged {result['staged']} version-marker restore(s) -> {out}", fg=typer.colors.GREEN)


@app.command()
def shazam_filter(
    review: Annotated[str, typer.Option(help="Fingerprint review / markers CSV to gate.")] = DEFAULT_REVIEW,
    verdicts: Annotated[
        str, typer.Option(help="Shazam verdicts CSV from scripts/shazam_verify.py.")
    ] = DEFAULT_VERDICTS,
    out: Annotated[str | None, typer.Option(help="Write result here instead of in place.")] = None,
) -> None:
    """Gate fingerprint proposals on a SECOND fingerprinter. A retitle from AcoustID alone can be a
    coin-flip; run scripts/shazam_verify.py (py3.12) over a `--fingerprint` scan or `markers` CSV to
    Shazam each file, then this downgrades every fingerprint row Shazam did not AGREE with to skip
    (HIGH -> REVIEW). Only two-fingerprinter-agreed retitles survive to adjudicate/apply."""
    result = operations.shazam_filter(review, verdicts, out)
    typer.secho(
        f"shazam-filter: {result['agree']} agreed, {result['downgraded']} downgraded, "
        f"{result['untouched']} non-fingerprint -> {result['review']}",
        fg=typer.colors.GREEN,
    )


@app.command(name="scrobble-check")
def scrobble_check(
    review: Annotated[str, typer.Option(help="Review CSV to gate.")] = DEFAULT_REVIEW,
    out: Annotated[str | None, typer.Option(help="Write result here instead of in place.")] = None,
) -> None:
    """Final gate: downgrade any title/artist change last.fm does NOT know -- it would orphan
    scrobbles (the last correctness gate). Needs a last.fm API key ($LASTFM_KEY /
    ~/.lastfm_key); the keyless page scraper can't answer track-level, so the gate is a no-op
    without a key. Run after adjudicate/shazam-filter, before apply."""
    result = operations.scrobble_check(review, out)
    _warn(result["warnings"])
    if result["checked"] == 0:
        typer.secho("scrobble-check: gate OFF (no last.fm key) or no title/artist changes", fg=typer.colors.YELLOW)
    else:
        typer.secho(
            f"scrobble-check: {result['checked']} checked, {result['downgraded']} downgraded -> {result['review']}",
            fg=typer.colors.GREEN,
        )


@app.command()
def apply(
    review: Annotated[str, typer.Option(help="Review CSV to apply.")] = DEFAULT_REVIEW,
    log: Annotated[str, typer.Option(help="Change log (undo source).")] = DEFAULT_LOG,
    dry_run: Annotated[bool, typer.Option(help="Report without writing.")] = False,
) -> None:
    """Apply kept rows from the review CSV, appending to the undo log."""
    _log_header(log)
    _print_direction(review)  # show WHAT will change before the write, scannable
    result = operations.apply(review, log, dry_run=dry_run)
    tag = "would apply" if dry_run else "applied"
    typer.secho(f"{tag} {result['applied']} changes, skipped {result['skipped']}", fg=typer.colors.GREEN)
    locked = result["locked"]
    if isinstance(locked, list) and locked:
        typer.secho(f"locked ({len(locked)}): {', '.join(locked[:5])}", fg=typer.colors.YELLOW)
    for err in result["errors"] if isinstance(result["errors"], list) else []:
        typer.secho(str(err), fg=typer.colors.RED)


@app.command()
def duplicates(
    root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None,
    limit: Annotated[int, typer.Option(help="How many groups to list.")] = 50,
) -> None:
    """Report files that share an artist+title (metadata duplicates), best-quality first."""
    result = operations.duplicates(_resolve_root(root), limit, progress=_progress())
    typer.secho(f"{result['groups']} duplicate groups", fg=typer.colors.GREEN)
    for g in result["sample"] if isinstance(result["sample"], list) else []:
        typer.echo(f"  {g['artist']} - {g['title']} ({g['count']}) -> keep {g['keep']}")


@app.command()
def doctor(root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None) -> None:
    """Report tag anomalies (blank fields, artist==title, multi-artist) without changing anything."""
    result = operations.doctor(_resolve_root(root), progress=_progress())
    issues = result["issues"]
    typer.secho(f"{issues} issues", fg=typer.colors.GREEN if not issues else typer.colors.YELLOW)
    by_type = result["by_type"]
    for issue, count in by_type.items() if isinstance(by_type, dict) else []:
        typer.echo(f"  {count:6} {issue}")


@app.command()
def coverart(
    root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None,
    mode: Annotated[
        str, typer.Option(help="'folder' = one cover.jpg per album folder (cheap); 'embed' = into each file.")
    ] = "folder",
    replace: Annotated[
        bool, typer.Option(help="embed mode: also replace art on files that already have some.")
    ] = False,
    dry_run: Annotated[bool, typer.Option(help="Report what would be fetched, write nothing.")] = False,
    log: Annotated[str, typer.Option(help="Change log (undo removes the added art).")] = DEFAULT_LOG,
) -> None:
    """Fetch cover art (Cover Art Archive -> iTunes). Default: one cover.jpg per album folder
    (small disk cost). Use --mode embed to embed a cover into every file that lacks one."""
    root = _resolve_root(root)
    _log_header(log)
    result = operations.coverart(root, mode=mode, replace=replace, dry_run=dry_run, log=log)
    if "error" in result:
        raise typer.BadParameter(str(result["error"]))
    noun = "covers embedded" if result["mode"] == "embed" else "folder covers"
    verb = "would write" if dry_run else "wrote"
    typer.secho(f"{verb} {result['written']} {noun}, skipped {result['skipped']}", fg=typer.colors.GREEN)
    locked = result["locked"]
    if isinstance(locked, list) and locked:
        typer.secho(f"locked ({len(locked)})", fg=typer.colors.YELLOW)
    for err in result["errors"] if isinstance(result["errors"], list) else []:
        typer.secho(str(err), fg=typer.colors.RED)


@app.command()
def rename(
    root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None,
    all_folders: Annotated[bool, typer.Option("--all", help="Also rename inside album folders.")] = False,
    stage: Annotated[
        str | None,
        typer.Option(help="Write the plan to this CSV and rename NOTHING (review it, then `apply-renames --plan`)."),
    ] = None,
    dry_run: Annotated[bool, typer.Option(help="Report the plan without renaming.")] = False,
    log: Annotated[str, typer.Option(help="Change log (undo restores the old name).")] = DEFAULT_LOG,
) -> None:
    """Rename files to 'artist - title', filesystem-safe. Skips album folders unless --all. A rename
    moves the path the undo log keys on, so prefer --stage: write a review CSV, check it, then
    `apply-renames --plan` -- the same discipline as tag edits."""
    root = _resolve_root(root)
    if stage:
        result = operations.rename(
            root, all_folders=all_folders, stage=stage, dry_run=dry_run, log=log, progress=_progress()
        )
        typer.secho(
            f"staged {result['staged']} rename(s) -> {stage} (review, then `apply-renames --plan {stage}`)",
            fg=typer.colors.GREEN,
        )
        return
    _log_header(log)
    result = operations.rename(
        root, all_folders=all_folders, stage=None, dry_run=dry_run, log=log, progress=_progress()
    )
    _report_renames(result, dry_run)


@app.command(name="apply-renames")
def apply_renames(
    plan: Annotated[str, typer.Option(help="A staged rename plan CSV (from `rename --stage`).")],
    dry_run: Annotated[bool, typer.Option(help="Report without renaming.")] = False,
    log: Annotated[str, typer.Option(help="Change log (undo restores the old name).")] = DEFAULT_LOG,
) -> None:
    """Apply a reviewed rename plan CSV, logging every rename so undo restores the old name."""
    _log_header(log)
    result = operations.apply_renames(plan, dry_run=dry_run, log=log)
    _report_renames(result, dry_run)


@app.command()
def albumartist(
    root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None,
    review: Annotated[str, typer.Option(help="Review CSV to write.")] = DEFAULT_REVIEW,
) -> None:
    """Fill a blank albumartist from the folder's dominant artist (single-artist albums only),
    staged to a review CSV. A blank albumartist splits an album in players that group by it."""
    result = operations.albumartist(_resolve_root(root), review, progress=_progress())
    typer.secho(f"staged {result['staged']} albumartist fills -> {review}", fg=typer.colors.GREEN)


@app.command()
def scrobble_names(
    root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None,
    review: Annotated[str, typer.Option(help="Review CSV to write.")] = DEFAULT_REVIEW,
) -> None:
    """Retag each artist to the spelling with the most last.fm scrobbles (its most-listened
    MusicBrainz alias) so scrobbles stop orphaning. Works with no key (scrapes the public last.fm
    page); set $LASTFM_KEY / ~/.lastfm_key for the JSON API. Staged to a review CSV; a name
    rewrite is always REVIEW."""
    result = operations.scrobble_names(_resolve_root(root), review, progress=_progress())
    _warn(result["warnings"])
    typer.secho(f"staged {result['staged']} scrobble-name retags -> {review}", fg=typer.colors.GREEN)


@app.command()
def changes(
    limit: Annotated[int, typer.Option(help="How many to show, newest first (0 = all).")] = 20,
    log: Annotated[str, typer.Option(help="Change log.")] = DEFAULT_LOG,
) -> None:
    """List the undo-able changes, newest first. Each line starts with the id `undo --id` takes,
    then the run id that groups everything one command wrote (`undo --run`)."""
    _log_header(log)
    rows = operations.list_changes(log, limit)
    if not rows:
        typer.echo("no changes logged")
        return
    for row in rows:
        typer.echo(_change_line(row))


@app.command()
def undo(
    n: Annotated[int, typer.Argument(help="How many recent changes to reverse (ignored with a selector).")] = 1,
    log: Annotated[str, typer.Option(help="Change log.")] = DEFAULT_LOG,
    ids: Annotated[
        list[str] | None, typer.Option("--id", help="Reverse this change id (repeatable; see `changes`).")
    ] = None,
    run: Annotated[str, typer.Option(help="Reverse every change one command wrote (a run id from `changes`).")] = "",
    path: Annotated[str, typer.Option(help="Reverse every logged change whose file matches this glob.")] = "",
    dry_run: Annotated[bool, typer.Option(help="Show what would be reversed, change nothing.")] = False,
) -> None:
    """Reverse applied changes: the ids given, or a whole run, or every change to a matching file --
    else the last n. --run and --path narrow each other, --id adds. Run it with --dry-run first to
    see the exact selection."""
    if (ids or run or path) and n != 1:
        # silently reverting a whole run under `undo 5 --run X` is the wrong kind of surprise
        raise typer.BadParameter("n and a selector do not combine -- drop the count, or the selector")
    _log_header(log)
    result = operations.undo(n, log, ids=ids, run=run, path=path, dry_run=dry_run)
    verb = "would reverse" if dry_run else "reverted"
    typer.secho(f"{verb} {result['reverted']} changes", fg=typer.colors.GREEN)
    for row in result["changes"] if isinstance(result["changes"], list) else []:
        typer.echo(f"  {_change_line(row)}")
    locked = result["locked"]
    if isinstance(locked, list) and locked:
        typer.secho(f"locked: {', '.join(locked)}", fg=typer.colors.YELLOW)
    for err in result["errors"] if isinstance(result["errors"], list) else []:
        typer.secho(str(err), fg=typer.colors.RED)


@app.command()
def clean(
    root: Annotated[str | None, typer.Argument(help="Library root (or $TAGISTRY_ROOT).")] = None,
    dry_run: Annotated[bool, typer.Option(help="Count without deleting.")] = False,
) -> None:
    """Delete orphaned atomic-write temp files ('*.tagistry.*.tmp') left when a write's own cleanup
    was blocked by a lock. They are never-swapped-in copies, so removing them is safe."""
    result = operations.clean(_resolve_root(root), dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    typer.secho(f"{verb} {result['deleted']} orphaned temp file(s)", fg=typer.colors.GREEN)
    locked = result["locked"]
    if isinstance(locked, list) and locked:
        typer.secho(f"still locked ({len(locked)}): {', '.join(locked[:3])}", fg=typer.colors.YELLOW)


@app.command()
def status(log: Annotated[str, typer.Option(help="Change log.")] = DEFAULT_LOG) -> None:
    """Show how many changes are logged and the most recent one."""
    info = operations.status(log)
    typer.echo(f"applied changes logged: {info['applied_changes']}")
    if info["last"]:
        typer.echo(f"last: {info['last']}")


@app.command()
def plex_refresh(
    url: Annotated[str | None, typer.Option(help="Plex base URL (or $PLEX_URL).")] = None,
    token: Annotated[str | None, typer.Option(help="Plex token (or $PLEX_TOKEN).")] = None,
) -> None:
    """Tell Plex to rescan its music sections so it picks up the tag/art edits (post-apply sink)."""
    result = operations.plex_refresh(url, token)
    if "error" in result:
        raise typer.BadParameter(str(result["error"]))
    typer.secho(f"refreshed {result['refreshed']} Plex music section(s)", fg=typer.colors.GREEN)


def main() -> None:
    """The console entry point. A closed stdout (`tagistry changes | head -5`) must exit quietly:
    the pipe raises on write -- EPIPE on unix, EINVAL on Windows -- and typer would print a
    traceback for what the user asked for."""
    import contextlib
    import sys

    try:
        app()
    except OSError:
        sys.stderr.close()  # else the interpreter reports the same failure again while flushing
        sys.exit(0)
    finally:
        with contextlib.suppress(OSError):
            sys.stdout.flush()


if __name__ == "__main__":
    main()
