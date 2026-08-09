# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `changes` lists every undo-able change, newest first, with the id that names it and the run id
  that groups everything one command wrote.
- `undo` selects: `--id` (repeatable), `--run`, `--path` (glob), or `--dry-run` to preview the
  selection. `--run` and `--path` narrow each other. With no selector it still reverses the last
  n. Same options on the MCP `undo` tool, plus a `list_changes` tool.

### Changed

- Each logged change now carries a `run` field naming the batch that wrote it. A log written
  before this reads back unchanged; those entries just have no run.
- `status` (CLI and MCP) reports its `last` change as a display row -- it gains `id`, `run` and a
  formatted `time`, and drops the raw `ts` and `digest`.
- The `log:` header line goes to stderr, so `changes` and `undo` pipe cleanly into grep or awk.

### Fixed

- Piping a command into `head` (or any reader that closes early) printed a traceback instead of
  exiting quietly. The console entry point is now `cli:main`, which swallows the closed-pipe error.
- `undo 5 --run X` silently ignored the 5 and reverted the whole run. It is an error now.
- `undo` refuses to revert a change alone when a later rename moved its file, and names the
  rename to undo first, instead of failing on a path that no longer exists.

## [0.1.0] - 2026-08-07

First release.

### Added

- `scan` reads a library and stages proposals to a review CSV; `apply` writes the rows you kept
  and logs each change; `undo` reverses them; `status` reports what is logged.
- Thirteen fixers: `multi_artist`, `feat_to_title`, `flip`, `merged_field`, `title_junk`,
  `album_junk`, `ascii_dash`, `normalize`, `resolve_artist`, `blank_id`, `canonicalize`,
  `genre_fill`, `year_fill`. Each proposal carries a confidence and the evidence behind it.
- Gates that re-decide the staged rows before `apply`: `adjudicate` (deterministic policies),
  `shazam-filter` (a second fingerprinter must agree), `scrobble-check` (last.fm must know the
  result), and `disambiguate` (a layer-2 agent, which must cite a source and may answer
  "uncertain").
- `rename` to `artist - title`, with `--stage` and `apply-renames` for the same review-then-apply
  discipline as tag edits; `coverart` for a folder sidecar or an embedded cover.
- Read-only reports: `duplicates`, `doctor`. Staged helpers: `albumartist`, `scrobble-names`.
  Housekeeping: `clean`, `plex-refresh`.
- `tagistry-mcp`, an MCP adapter over the same operations layer the CLI uses. Every tool that
  writes to disk defaults to `dry_run=True`.
- Providers: MusicBrainz, AcoustID, last.fm, Discogs, Cover Art Archive and iTunes, plus an
  offline LibraryPrior that treats your own library as ground truth. All cached and rate-limited.

### Safety

- Tag writes are atomic: a temp copy in the same directory, then `os.replace`, with the original
  mtime preserved. A crash never leaves a half-written file.
- Every write is appended to a JSONL change log and fsynced. `undo` reverses it, and a torn line
  costs only itself.
- `apply` and `undo` both refuse to touch a value that changed since it was recorded, so neither a
  stale review nor a stale undo can destroy a newer edit.
- Replacing a cover keeps every other embedded picture, so undo can restore what it replaced.
- Fixers that emit several rows for one file (`flip`, `feat_to_title`) apply together or not at
  all — a half-swap cannot corrupt a file.
- Only HIGH confidence auto-applies. Fingerprint-driven retags are never HIGH on one source alone.
- State resolves to `~/.tagistry`, never the working directory, so the undo log cannot scatter.
- Locked files are retried, then reported — never corrupted. A mislabeled file is reported as
  unreadable rather than as locked.

### Security

- Tag text is untrusted input. It is fenced as data before reaching an LLM prompt, escaped before
  reaching the review CSV (a leading `=`, `+`, `-` or `@` is a spreadsheet formula), length-capped
  before the title regexes, and sanitised before becoming a filename.
- Provider credentials are read from env vars or `~/.*_key` files and stripped from the on-disk
  HTTP cache.

[0.1.0]: https://github.com/el-f/tagistry/releases/tag/v0.1.0
