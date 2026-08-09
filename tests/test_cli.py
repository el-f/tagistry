"""CLI wiring: every command runs hermetically via CliRunner (offline, --block-network).

The unit suites exercise the pure core; these prove the typer layer wires flags -> providers ->
pipeline correctly and that each command's error path surfaces (a bad root, an unknown fixer, a
missing Plex token) instead of a stack trace.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mediafile import MediaFile
from typer.testing import CliRunner

from tagistry import pipeline
from tagistry.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _lib(
    tmp_path: Path, name: str = "song.mp3", artist: str = "Radiohead", title: str = "Karma Police (Remastered)"
) -> str:
    path = tmp_path / name
    shutil.copy2(FIXTURES / "sample.mp3", path)
    mf = MediaFile(str(path))
    mf.artist, mf.title = artist, title
    mf.save()
    return str(tmp_path)


def _staged_review(tmp_path: Path, root: str) -> str:
    """An offline scan's review CSV -- the input the staging commands operate on."""
    review = str(tmp_path / "r.csv")
    assert runner.invoke(app, ["scan", root, "--review", review, "--no-online"]).exit_code == 0
    return review


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the config base dir at an empty temp + drop the ambient env, so a test never reads the
    developer's real ~/.tagistry/config.toml, $TAGISTRY_ROOT, or a ~/.lastfm_key."""
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path / "cfg"))
    for var in ("TAGISTRY_ROOT", "LASTFM_KEY", "DISCOGS_TOKEN", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")  # no ~/.lastfm_key etc.


def test_scan_apply_undo_status(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review = str(tmp_path / "r.csv")
    log = str(tmp_path / "c.jsonl")

    r = runner.invoke(app, ["scan", root, "--review", review, "--no-online"])
    assert r.exit_code == 0, r.output
    assert "staged" in r.output
    song = next(Path(root).glob("*.mp3"))

    r = runner.invoke(app, ["review", "--review", review])
    assert r.exit_code == 0

    r = runner.invoke(app, ["apply", "--review", review, "--log", log])
    assert r.exit_code == 0
    assert MediaFile(str(song)).title == "Karma Police"

    r = runner.invoke(app, ["status", "--log", log])
    assert "applied changes logged: 1" in r.output

    r = runner.invoke(app, ["undo", "1", "--log", log])
    assert r.exit_code == 0
    assert MediaFile(str(song)).title == "Karma Police (Remastered)"


def test_changes_lists_an_id_that_undo_takes(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    song = next(Path(root).glob("*.mp3"))
    assert runner.invoke(app, ["scan", root, "--review", review, "--no-online"]).exit_code == 0
    assert runner.invoke(app, ["apply", "--review", review, "--log", log]).exit_code == 0

    listed = runner.invoke(app, ["changes", "--log", log])
    change_id = pipeline.list_changes(log)[0]["id"]
    assert change_id in listed.output and "title:" in listed.output

    preview = runner.invoke(app, ["undo", "--log", log, "--id", change_id, "--dry-run"])
    assert "would reverse 1 changes" in preview.output
    assert MediaFile(str(song)).title == "Karma Police"  # a preview writes nothing

    r = runner.invoke(app, ["undo", "--log", log, "--id", change_id])
    assert r.exit_code == 0 and MediaFile(str(song)).title == "Karma Police (Remastered)"
    assert "no changes logged" in runner.invoke(app, ["changes", "--log", log]).output


def test_undo_refuses_a_count_together_with_a_selector(tmp_path: Path) -> None:
    # `undo 5 --run X` reverting the whole run regardless of the 5 is the wrong kind of surprise
    r = runner.invoke(app, ["undo", "5", "--log", str(tmp_path / "c.jsonl"), "--run", "abc123"])
    assert r.exit_code != 0 and "do not combine" in r.output


def test_reapply_redoes_exactly_the_undone_change(tmp_path: Path) -> None:
    """There is no `redo` command: apply skips rows already matching, so re-running it restores
    what was undone and nothing else. If that stops holding, redo becomes a real gap."""
    root = _lib(tmp_path, name="a.mp3")
    shutil.copy2(FIXTURES / "sample.mp3", tmp_path / "b.mp3")
    mf = MediaFile(str(tmp_path / "b.mp3"))
    mf.artist, mf.title = "Radiohead", "Creep (Remastered)"
    mf.save()
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    assert runner.invoke(app, ["scan", root, "--review", review, "--no-online"]).exit_code == 0
    assert runner.invoke(app, ["apply", "--review", review, "--log", log]).exit_code == 0

    undone = next(r for r in pipeline.list_changes(log) if r["path"].endswith("b.mp3"))
    assert runner.invoke(app, ["undo", "--log", log, "--id", undone["id"]]).exit_code == 0
    assert MediaFile(str(tmp_path / "b.mp3")).title == "Creep (Remastered)"

    again = runner.invoke(app, ["apply", "--review", review, "--log", log])
    assert "applied 1 changes, skipped 1" in again.output  # only the undone row was rewritten
    assert MediaFile(str(tmp_path / "b.mp3")).title == "Creep"
    assert MediaFile(str(tmp_path / "a.mp3")).title == "Karma Police"


def test_scan_fixers_subset_only_runs_named(tmp_path: Path) -> None:
    from tagistry import pipeline

    root = _lib(tmp_path)  # title "Karma Police (Remastered)" -> only title_junk proposes here
    r1, r2 = str(tmp_path / "r1.csv"), str(tmp_path / "r2.csv")
    assert runner.invoke(app, ["scan", root, "--review", r1, "--no-online", "--fixers", "title_junk"]).exit_code == 0
    assert runner.invoke(app, ["scan", root, "--review", r2, "--no-online", "--fixers", "normalize"]).exit_code == 0
    named = {row.fixer for row in pipeline.read_review(r1)}
    other = {row.fixer for row in pipeline.read_review(r2)}
    assert "title_junk" in named  # the named fixer ran
    assert "title_junk" not in other  # excluded when a different fixer is named


def test_scan_resume_reports_nothing_new(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review = str(tmp_path / "r.csv")
    assert runner.invoke(app, ["scan", root, "--review", review, "--no-online"]).exit_code == 0
    resumed = runner.invoke(app, ["scan", root, "--review", review, "--no-online", "--resume"])
    assert resumed.exit_code == 0, resumed.output
    assert "+0 new this run" in resumed.output  # the file was already processed -> the resume render path


def test_scan_resume_skips_clean_files_via_marker(tmp_path: Path) -> None:
    # A CLEAN file leaves no CSV row, so resume tracks it in the .processed sidecar and skips it next run.
    root = _lib(tmp_path, artist="U2", title="One")  # offline: no fixer proposes -> a clean file
    review = str(tmp_path / "r.csv")
    first = runner.invoke(app, ["scan", root, "--review", review, "--no-online", "--resume"])
    assert first.exit_code == 0, first.output
    marker = Path(review + ".processed")
    song = next(Path(root).glob("*.mp3"))
    assert marker.exists() and str(song) in marker.read_text(encoding="utf-8")  # clean file recorded
    resumed = runner.invoke(app, ["scan", root, "--review", review, "--no-online", "--resume"])
    assert resumed.exit_code == 0
    assert "+0 new this run" in resumed.output  # skipped via the marker, not re-scanned


def test_apply_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review = str(tmp_path / "r.csv")
    log = str(tmp_path / "c.jsonl")
    runner.invoke(app, ["scan", root, "--review", review, "--no-online"])
    song = next(Path(root).glob("*.mp3"))

    r = runner.invoke(app, ["apply", "--review", review, "--log", log, "--dry-run"])
    assert r.exit_code == 0 and "would apply" in r.output
    assert MediaFile(str(song)).title == "Karma Police (Remastered)"  # untouched
    assert not Path(log).exists()


# --- read-only reports over a library ---------------------------------------


def test_doctor_reports_anomalies(tmp_path: Path) -> None:
    root = _lib(tmp_path, artist="", title="")  # blank artist + title => anomalies
    r = runner.invoke(app, ["doctor", root])
    assert r.exit_code == 0 and "issues" in r.output


def test_duplicates_groups_same_artist_title(tmp_path: Path) -> None:
    _lib(tmp_path, name="a.mp3", artist="U2", title="One")
    _lib(tmp_path, name="b.mp3", artist="U2", title="One")  # a metadata duplicate
    r = runner.invoke(app, ["duplicates", str(tmp_path)])
    assert r.exit_code == 0 and "1 duplicate groups" in r.output
    assert "U2 - One (2)" in r.output and "keep a.mp3" in r.output  # the group and its keep hint


# --- staging passes over a review CSV ---------------------------------------


def test_adjudicate_and_review_and_markers(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review = _staged_review(tmp_path, root)

    r = runner.invoke(app, ["review", "--review", review])
    assert r.exit_code == 0 and "proposals in" in r.output

    r = runner.invoke(app, ["adjudicate", "--review", review])
    assert r.exit_code == 0 and "adjudicated" in r.output

    out = str(tmp_path / "m.csv")
    r = runner.invoke(app, ["markers", "--review", review, "--out", out])
    assert r.exit_code == 0 and "marker" in r.output


def test_shazam_filter_gates_on_verdicts(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review = _staged_review(tmp_path, root)
    verdicts = tmp_path / "v.csv"
    verdicts.write_text("path,verdict\n", encoding="utf-8")  # no AGREE for any path
    r = runner.invoke(app, ["shazam-filter", "--review", review, "--verdicts", str(verdicts)])
    assert r.exit_code == 0 and "shazam-filter" in r.output


def test_scrobble_check_gate_off_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch, tmp_path)
    root = _lib(tmp_path)
    review = _staged_review(tmp_path, root)
    r = runner.invoke(app, ["scrobble-check", "--review", review])
    assert r.exit_code == 0 and "gate OFF" in r.output  # no key -> page scraper -> no network


def test_disambiguate_with_no_researcher_is_a_noop(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    review = _staged_review(tmp_path, root)
    r = runner.invoke(app, ["disambiguate", "--review", review, "--researcher", "none", "--no-online"])
    assert r.exit_code == 0 and "touched 0 rows" in r.output


# --- rename (stage -> apply) + albumartist + clean --------------------------


def test_rename_stage_then_apply_renames(tmp_path: Path) -> None:
    root = _lib(tmp_path, artist="Wham!", title="Freedom")
    plan = str(tmp_path / "plan.csv")
    log = str(tmp_path / "c.jsonl")  # DEFAULT_LOG is import-time; pass --log so we never touch the real one
    r = runner.invoke(app, ["rename", root, "--stage", plan])
    assert r.exit_code == 0 and "staged" in r.output
    assert Path(plan).exists()

    r = runner.invoke(app, ["apply-renames", "--plan", plan, "--log", log])
    assert r.exit_code == 0 and "renamed 1" in r.output
    assert (Path(root) / "Wham! - Freedom.mp3").exists()
    assert Path(log).exists()  # the rename was logged to OUR temp log, not the default


def test_albumartist_stages_offline(tmp_path: Path) -> None:
    _lib(tmp_path, name="1.mp3", artist="Rush", title="Tom Sawyer")
    _lib(tmp_path, name="2.mp3", artist="Rush", title="Limelight")
    review = tmp_path / "r.csv"
    r = runner.invoke(app, ["albumartist", str(tmp_path), "--review", str(review)])
    assert r.exit_code == 0 and "albumartist fills" in r.output
    rows = pipeline.read_review(str(review))
    assert rows and all(row.field == "albumartist" and row.proposed == "Rush" for row in rows)


def test_clean_removes_orphan_temp(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    (Path(root) / "song.mp3.tagistry.deadbeef.tmp").write_text("orphan", encoding="utf-8")
    r = runner.invoke(app, ["clean", root])
    assert r.exit_code == 0 and "deleted 1" in r.output
    assert not (Path(root) / "song.mp3.tagistry.deadbeef.tmp").exists()  # the effect, not the message


# --- error paths surface as clean messages, not tracebacks ------------------


def test_missing_root_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch, tmp_path)  # no $TAGISTRY_ROOT, no config.toml
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code != 0 and "no library root" in r.output


def test_scan_unknown_fixer_errors(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    r = runner.invoke(app, ["scan", root, "--no-online", "--fixers", "does_not_exist"])
    assert r.exit_code != 0


def test_coverart_rejects_a_bad_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch, tmp_path)  # the fetcher's cache dir resolves at call time -> tmp, not real config
    root = _lib(tmp_path)
    r = runner.invoke(app, ["coverart", root, "--mode", "bogus", "--dry-run"])
    assert r.exit_code != 0


def test_plex_refresh_requires_url_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch, tmp_path)
    r = runner.invoke(app, ["plex-refresh"])
    assert r.exit_code != 0


def test_verbose_flag_runs(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    r = runner.invoke(app, ["--verbose", "scan", root, "--review", str(tmp_path / "r.csv"), "--no-online"])
    assert r.exit_code == 0
