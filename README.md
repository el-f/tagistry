# Tagistry

[![gate](https://github.com/el-f/tagistry/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/el-f/tagistry/actions/workflows/ci.yml)

A music **tag-correction engine**. It fixes an already-organized but messy music library
in place: detect a tag mistake, propose a fix with confidence and evidence, verify it
against ground truth, stage a review file, then apply reversibly with an undo log.

Not a library manager (that is [beets](https://beets.io)). Tagistry is the narrow slice
beets does not do — flip detection, merged-field repair, multi-artist disambiguation,
in-title junk strip — and it reuses beets' libraries (`mediafile`, `pyacoustid`) for the
commodity parts.

## Install

Not on PyPI. Install from git:

```bash
uv tool install "git+https://github.com/el-f/tagistry"
uv tool install "tagistry[mcp] @ git+https://github.com/el-f/tagistry"   # also the MCP adapter
```

Python 3.13+. Check it works: `tagistry --version`.

Fingerprinting (`--fingerprint`) additionally needs:

- the [`fpcalc`](https://acoustid.org/chromaprint) binary on `PATH` (or point `$FPCALC` at it)
- a free [AcoustID API key](https://acoustid.org/new-application) in `~/.acoustid_key`

### State location

The change log, review CSV, and provider caches resolve to one base dir — **never the current
working directory** (an `apply` with no `--log` must not scatter the undo net). Precedence:
`$TAGISTRY_DIR`, a `config.toml` in it, else `~/.tagistry`. Every write prints its resolved path.

```
~/.tagistry/changes.jsonl   the undo log
~/.tagistry/review.csv      the staged proposals
~/.tagistry/cache/          provider caches (SQLite)
```

`~/.tagistry/config.toml` takes three keys, each an absolute path: `root`, `log`, `review`.

| Environment variable | What it sets |
|---|---|
| `TAGISTRY_DIR` | the base dir (default `~/.tagistry`) |
| `TAGISTRY_ROOT` | a default library root, so commands can omit it |
| `TAGISTRY_LOG` / `TAGISTRY_REVIEW` | override those two paths |
| `TAGISTRY_LOG_LEVEL` | `DEBUG` surfaces per-file fixer errors (same as `--verbose`) |
| `FPCALC` | path to the `fpcalc` binary |
| `LASTFM_KEY` | last.fm API key (or `~/.lastfm_key`) |
| `DISCOGS_TOKEN` | Discogs token (or `~/.discogs_token`) |
| `RESEARCHER_BASE_URL` / `RESEARCHER_MODEL` / `RESEARCHER_API_KEY` | the `--researcher http` backend (or `~/.researcher_key`) |
| `PLEX_URL` / `PLEX_TOKEN` | for `plex-refresh` |

## Workflow

The flow is always **scan → review → apply → undo**. Nothing is written until you say so,
and every write is reversible.

```bash
tagistry scan ~/Music                    # stage proposals to ~/.tagistry/review.csv
#   open the CSV, check each row, set the `apply` column to apply/skip
tagistry review                          # summary of what's staged
tagistry apply                           # write kept rows, log each change
tagistry changes                         # list what is undo-able, newest first
tagistry undo 5                          # reverse the last 5 changes
tagistry status                          # how many changes are logged
```

### Undoing one change, not the last five

`tagistry changes` prints every undo-able change, newest first. Each line starts with the
change's own **id**, then the **run** id shared by everything one command wrote:

```
7f3c1a09b2d4  2026-08-09 14:02:11  run 4b8e02d1  Karma Police.mp3  title: 'Karma Police (Remastered)' -> 'Karma Police'
```

Undo takes either one:

```bash
tagistry undo --id 7f3c1a09b2d4                    # just that change
tagistry undo --run 4b8e02d1                       # everything one apply/rename/coverart wrote
tagistry undo --path '*Radiohead*'                 # every logged change to matching files
tagistry undo --run 4b8e02d1 --path '*.flac'       # that run, only the FLACs
tagistry undo --run 4b8e02d1 --dry-run             # show the selection, change nothing
```

Ids come from the change's content, so they stay valid as the log shrinks. `--run` and `--path`
narrow each other; `--id` adds named changes on top. With no selector, `undo n` still means "the
last n". Changes revert newest-first, so a run that renamed *and* retagged a file unwinds in the
right order; undoing an older change **alone** after a later rename moved its file is refused,
and tells you which rename to undo first.

**Undid the wrong one?** Run the same command again — there is no separate redo, because you
don't need one. `apply` only writes rows whose current value still matches the CSV, so re-running
it restores exactly what you undid and skips everything else. Same for `apply-renames --plan` and
`coverart`.

`scan` flags: `--fixers multi_artist,flip` (subset), `--no-online` (skip MusicBrainz),
`--fingerprint` (AcoustID: `blank_id` + `canonicalize` + `year_fill`), `--discogs`
(`genre_fill`), `--resume` (survive a Ctrl-C), `--researcher` (see below).
`apply` takes `--dry-run`.

`apply` refuses a row whose field changed on disk since the scan, and `undo` refuses to revert
a value someone edited after Tagistry wrote it. Both report the conflict instead of overwriting.

### Rename files to `artist - title`

After the tags are clean, rename the files to match. Filesystem-safe (illegal characters mapped,
accents kept), and every rename is logged so `undo` restores the old name.

```bash
tagistry rename ~/Music --stage plan.csv   # write a reviewable plan, rename nothing
tagistry apply-renames --plan plan.csv     # apply the reviewed plan
tagistry rename ~/Music --all              # or rename in place, incl. inside album folders
```

A rename moves the path the undo log keys on, so `--stage` then `apply-renames` is the safer
route — the same review discipline as tag edits. An **album folder** (most files share one album
tag + track-number filenames) is skipped unless `--all`, so its ordering survives.

### Cover art

Fetch cover art — Cover Art Archive (by release MBID) first, then iTunes (by artist + title).
Opt-in only: `scan`/`apply`/`rename` never touch art.

The default writes **one `cover.jpg` per album folder** (cheap on disk, and Plex/Kodi/most
players read it). `--mode embed` embeds a cover into every file that lacks one (~50-150 KB each).
Both are logged, so `undo` deletes the sidecar / removes the embedded art. Replacing a cover
keeps every other embedded picture (back art, booklet scans) untouched.

```bash
tagistry coverart ~/Music                 # default: one cover.jpg per album folder
tagistry coverart ~/Music --dry-run       # report how many, write nothing
tagistry coverart ~/Music --mode embed    # embed into each art-less file
```

Folders that already have a `cover.jpg`/`folder.jpg`/`front.jpg` are skipped.

### Layer-2 researcher (hard residue)

Cross-script transliterations and obscure acts that the deterministic checks can't settle can be
handed to a layer-2 agent. It must cite a source and may answer "uncertain" — it never guesses,
and its answer is cross-verified against MusicBrainz.

```bash
tagistry scan ~/Music --researcher cli    # resolve_artist asks `claude -p` for the residue
tagistry disambiguate --researcher cli    # let the agent confirm/correct REVIEW rows in a CSV
```

Backends: `none` (default — declines everything, offline behavior unchanged), `cli` (shells out
to `claude -p`, no API key), `http` (an OpenAI- or Anthropic-wire endpoint; needs
`$RESEARCHER_BASE_URL`, `$RESEARCHER_MODEL` and `$RESEARCHER_API_KEY`), and `majority` (asks
several and takes the agreed answer).

Tag text is untrusted input, so it is fenced as data in the prompt and the model's answer is
validated before it can become a proposed change.

**Deep re-tag from the audio** — rebuild every tag against the fingerprinted MusicBrainz
recording (catches remix/feat/live versions, artist joins). Slow (fingerprints every file),
all REVIEW, so nothing auto-applies. A fingerprint from ONE source can be a coin-flip, so gate it:

```bash
tagistry scan ~/Music --fingerprint --resume   # blank_id + canonicalize + year_fill
tagistry markers                               # narrow to titles that restore a stripped marker
uv run --with shazamio python scripts/shazam_verify.py \
    --review ~/.tagistry/markers.csv --out ~/.tagistry/shazam_verdicts.csv
tagistry shazam-filter                         # downgrade whatever the 2nd fingerprinter didn't confirm
tagistry adjudicate                            # deterministic policies
tagistry scrobble-check                        # drop any title/artist last.fm doesn't know (needs a key)
tagistry apply                                 # write only what survived every gate
```

Only a change that TWO fingerprinters agree on, the policies pass, and last.fm knows
survives to `apply`. Every stage prints a one-line DIRECTION digest of what it decided.

`scripts/shazam_verify.py` is a helper, not part of the package: it needs `shazamio`, which pins
an older Python, so run it with `uv run --with shazamio` under a 3.12 interpreter as shown.

### The review CSV

One row per proposed change:

```
apply, fixer, confidence, path, field, current, proposed, evidence, file_artist, file_title
```

The `apply` column defaults to `apply` for **HIGH** confidence and `skip` for **REVIEW/LOW** —
so a plain `apply` writes only the safe changes. Edit the column to include or exclude any row.
It keeps the original value, so it doubles as the rollback record. `file_artist`/`file_title` are
the file's identity at scan time, so a row is reviewable without opening the file.

Confidence: **HIGH** auto-applies; **REVIEW** / **LOW** need you to opt in. Every proposal
carries evidence (why the fixer suggested it).

## Fixers

| fixer | fixes | how it verifies |
|---|---|---|
| `multi_artist` | `"2Pac, Outlawz"` → `2Pac` (but keeps real bands: `Earth, Wind & Fire`) | MusicBrainz artist-credit + library prior; feat-token priority |
| `feat_to_title` | `artist "The Weeknd feat. Ariana"` → `artist "The Weeknd"` + `title "… (feat. Ariana)"` | feat belongs in the title (last.fm/MB convention); MusicBrainz must confirm the primary made the song |
| `flip` | swapped artist/title: `45 / Shinedown` → `Shinedown / 45` | title is a known artist + MusicBrainz confirms the swapped recording (rejects soundtrack traps) |
| `merged_field` | `"Miley Cyrus - Jolene"` in the title field → `Jolene` | the artist tag confirms the stripped prefix |
| `title_junk` | `Karma Police (Remastered)` → `Karma Police`; `(Official Video)`; `01. ` prefixes | regex corpus; keeps real parentheticals like `(Are Made of This)`, and a number that is part of the title (`1 - 800 - 273 - 8255`) |
| `album_junk` | `Unknown Album]` → cleared; stray brackets; `(2011 Remaster)` | placeholder list; keeps real editions like `(Deluxe Edition)` |
| `ascii_dash` | unicode dashes (`–` `—`) → ASCII `-` in artist and title | deterministic |
| `normalize` | fullwidth punctuation → ASCII (`Da＊＊it Now` → `Da**it Now`), spaced censor words, doubled spaces | deterministic; makes tags match the canonical last.fm/MB entry so scrobbles stop orphaning |
| `resolve_artist` | a blank or unresolvable artist, handed to the layer-2 researcher | agent answer, cross-verified against MusicBrainz (`--researcher`, REVIEW) |
| `blank_id` | blank **or** wrong tags on a suspicious file (placeholder album) | AcoustID fingerprint → MusicBrainz canonical; HIGH only when both agree, else REVIEW |
| `canonicalize` | rewrites artist/title to the MusicBrainz canonical for the recording — catches remix/feat/live versions that only the audio reveals (`--fingerprint`, REVIEW) | AcoustID → MB recording; the canonical title carries `(Remix)` / `(feat. X)` / `(Live)` |
| `genre_fill` | fills a **blank** genre from Discogs' curated genres (`--discogs`, REVIEW) | Discogs release match (`$DISCOGS_TOKEN`) |
| `year_fill` | fills a **blank** year from the recording's earliest MusicBrainz release (`--fingerprint`, REVIEW) | AcoustID → MB earliest release date |

## Safety

- **Atomic writes.** Tags are written into a temp copy in the same folder, then `os.replace`d
  in — a crash never leaves a half-written file. The original mtime is preserved.
- **Locked files.** If a player holds the file, Tagistry retries, then reports it (never corrupts).
- **Reversible.** Every applied change is appended to `changes.jsonl`; `tagistry undo` reverses
  them, by id, by run, by path, or just the last n. Applying twice is a no-op.
- **Never clobbers a newer edit.** `apply` and `undo` both check the file still holds the value
  they recorded, and report a conflict instead of overwriting.
- **HIGH-only auto-apply.** REVIEW/LOW never apply unless you opt in per row.

## Audits (read-only)

```bash
tagistry duplicates ~/Music   # files sharing an artist+title, best-quality first (keep hint)
tagistry doctor ~/Music       # blank fields, artist==title, multi-artist -- report only
```

Also read-only or staged: `albumartist` (fill a blank albumartist from the folder's dominant
artist), `scrobble-names` (retag each artist to its most-scrobbled last.fm spelling),
`clean` (delete orphaned `*.tagistry.*.tmp` files), `plex-refresh` (tell Plex to rescan).

`tagistry --help` lists all 19 commands.

## Providers

- **LibraryPrior** — offline. Your library is the ground truth: an artist seen ≥2 times is "known".
- **MusicBrainz** — ws/2 JSON, cached (SQLite) + rate-limited to 1 req/s, jittered retry backoff.
- **AcoustID** — audio fingerprint via `pyacoustid` + `fpcalc`; memoized per file within a scan.
- **last.fm** — a key (`$LASTFM_KEY` / `~/.lastfm_key`, free at
  <https://www.last.fm/api/account/create>) switches on the JSON API. That's what makes the
  `scrobble-check` gate work: it asks last.fm whether each resulting (artist, title) is known, and
  drops any retag last.fm doesn't recognize — so a "fix" can't orphan your scrobbles. Without a
  key it reads the public artist page instead (brittle, artist-level only), so `scrobble-check`
  is a no-op.
- **Discogs** — curated genres on a `$DISCOGS_TOKEN` (the `genre_fill` fixer).

Data comes from [MusicBrainz](https://musicbrainz.org), [AcoustID](https://acoustid.org),
[Cover Art Archive](https://coverartarchive.org), [Discogs](https://www.discogs.com),
[last.fm](https://www.last.fm) and the iTunes Search API. Each is used within its rate limits
and identified by a Tagistry User-Agent.

## Agent use (MCP)

`tagistry-mcp` exposes the same operations as MCP tools, each a passthrough to the one
orchestration layer the CLI uses, so the two cannot drift. Every tool that writes to disk
(`apply`, `rename`, `apply_renames`, `coverart`, `clean`) defaults to `dry_run=True`, so an
agent cannot edit files unreviewed — pass `dry_run=False` deliberately.

## Building it

```bash
git clone https://github.com/el-f/tagistry && cd tagistry
uv sync
./scripts/verify.sh    # ruff + ruff format + mypy --strict + pytest --block-network
```

Tests are hermetic and never touch the network (`--block-network` in CI): fixers are
golden-tested, the MusicBrainz provider runs against recorded vcrpy cassettes, the other
providers against injected fakes, and tag I/O against three ~0.3-second ffmpeg-generated audio
samples in `tests/fixtures/`. Mutation testing (`mutmut`, Linux-only) runs on demand.

## License

Copyright (C) 2026 Elazar Fine.

GPL-2.0-**or-later**, because it links `mutagen` (GPL-2.0-or-later). `mediafile` and
`pyacoustid` are MIT; `requests` is Apache-2.0, which is compatible under the GPL-3.0 leg of
"or later". See [LICENSE](LICENSE).

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY.
It rewrites tags in your music library — keep backups, and read the review CSV before `apply`.
