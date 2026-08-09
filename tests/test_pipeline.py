"""End-to-end flow: scan -> review CSV -> apply -> undo, plus dedup + idempotency."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from mediafile import MediaFile

from tagistry import changelog, pipeline, tagio
from tagistry.domain import Confidence, Evidence, FileLockedError, Proposal, Track, make_proposal
from tagistry.fixers import FIXERS
from tagistry.providers import Providers

FIXTURES = Path(__file__).parent / "fixtures"


def _make(dir_: Path, name: str, **tags: str) -> str:
    path = dir_ / name
    shutil.copy2(FIXTURES / "sample.mp3", path)
    mf = MediaFile(str(path))
    for k, v in tags.items():
        setattr(mf, k, v)
    mf.save()
    return str(path)


@pytest.fixture
def library(tmp_path: Path) -> dict[str, str]:
    """A temp library exercising each offline-eligible fixer."""
    return {
        "junk": _make(tmp_path, "a.mp3", artist="Radiohead", title="Karma Police (Remastered)"),
        "video": _make(tmp_path, "b.mp3", artist="Billie Eilish", title="Bad Guy (Official Video)"),
        "dash": _make(tmp_path, "c.mp3", artist="Foo", title="Song – Live"),
        "merged": _make(tmp_path, "d.mp3", artist="Coldplay", title="Coldplay - Yellow"),
        "compose": _make(tmp_path, "e.mp3", artist="Bar", title="Intro – Skit (Official Video)"),
        "clean": _make(tmp_path, "f.mp3", artist="U2", title="One"),
    }


def test_scan_reports_progress_once_per_file(library: dict[str, str]) -> None:
    root = str(Path(library["junk"]).parent)
    seen: list[tuple[int, int, str]] = []
    pipeline.scan(root, Providers(), progress=lambda done, total, path: seen.append((done, total, path)))
    total = len({*library.values()})
    assert len(seen) == total  # one callback per file
    assert [d for d, _t, _p in seen] == list(range(1, total + 1))  # 1..N in order
    assert all(t == total for _d, t, _p in seen)  # total is stable


def test_scan_survives_a_crashing_progress_callback(library: dict[str, str]) -> None:
    root = str(Path(library["junk"]).parent)

    def boom(done: int, total: int, path: str) -> None:
        raise UnicodeEncodeError("charmap", "x", 0, 1, "cp1252 stdout can't encode this")

    # a cosmetic progress error (e.g. a non-ASCII filename on a cp1252 console) must not abort
    proposals = pipeline.scan(root, Providers(), progress=boom)
    assert proposals  # scan still produced results


def test_scan_offline_produces_high_proposals(library: dict[str, str]) -> None:
    root = str(Path(library["junk"]).parent)
    proposals = pipeline.scan(root, Providers())
    by_path = {p.track_path: p for p in proposals if p.field == "title"}
    assert by_path[library["junk"]].proposed == "Karma Police"
    assert by_path[library["video"]].proposed == "Bad Guy"
    assert by_path[library["dash"]].proposed == "Song - Live"
    assert by_path[library["merged"]].proposed == "Yellow"
    # composition: junk strip wins, then the surviving en dash is normalized
    assert by_path[library["compose"]].proposed == "Intro - Skit"
    assert library["clean"] not in by_path


def test_full_apply_undo_cycle(tmp_path: Path, library: dict[str, str]) -> None:
    root = str(Path(library["junk"]).parent)
    review = str(tmp_path / "review.csv")
    log = str(tmp_path / "changes.jsonl")

    pipeline.write_review(pipeline.scan(root, Providers()), review)
    result = pipeline.apply(review, log)
    # exactly 5 HIGH title changes (junk, video, dash, merged, compose); clean file untouched
    assert result.applied == 5 and not result.locked and not result.errors

    assert MediaFile(library["junk"]).title == "Karma Police"
    assert MediaFile(library["dash"]).title == "Song - Live"
    assert MediaFile(library["merged"]).title == "Yellow"
    assert MediaFile(library["video"]).title == "Bad Guy"
    assert MediaFile(library["compose"]).title == "Intro - Skit"

    # idempotent: re-apply writes nothing
    again = pipeline.apply(review, log)
    assert again.applied == 0

    # undo reverses every field back to its exact original
    logged = pipeline.status(log)["applied_changes"]
    assert logged == 5
    undone = pipeline.undo(log, logged)
    assert undone.applied == 5
    assert MediaFile(library["junk"]).title == "Karma Police (Remastered)"
    assert MediaFile(library["dash"]).title == "Song – Live"
    assert MediaFile(library["merged"]).title == "Coldplay - Yellow"
    assert MediaFile(library["video"]).title == "Bad Guy (Official Video)"
    assert pipeline.status(log)["applied_changes"] == 0


def test_apply_only_writes_kept_rows(tmp_path: Path, library: dict[str, str]) -> None:
    root = str(Path(library["junk"]).parent)
    review = str(tmp_path / "r.csv")
    log = str(tmp_path / "c.jsonl")
    pipeline.write_review(pipeline.scan(root, Providers()), review)

    lines = Path(review).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines[1:], 1):  # skip header
        if line.startswith("apply,"):
            lines[i] = "skip," + line[len("apply,") :]
            break
    Path(review).write_text("\n".join(lines) + "\n", encoding="utf-8")

    kept = sum(1 for r in pipeline.read_review(review) if r.is_apply)
    result = pipeline.apply(review, log)
    assert result.applied == kept


def _p(path: str, field: str, proposed: str, conf: Confidence, fixer: str) -> Proposal:
    return Proposal(path, field, "cur", proposed, conf, Evidence(fixer, 100, "r"), fixer)


def test_scan_checkpoint_skips_already_processed_files(tmp_path: Path) -> None:
    _make(tmp_path, "a.mp3", artist="Radiohead", title="Karma Police (Remastered)")
    review = str(tmp_path / "r.csv")
    first = pipeline.scan(str(tmp_path), Providers(), checkpoint=review)
    assert first  # a produced a proposal, appended to the checkpoint CSV
    rows_after_first = pipeline.read_review(review)
    second = pipeline.scan(str(tmp_path), Providers(), checkpoint=review)  # re-run
    assert second == []  # a is already staged -> skipped, no new proposals
    assert len(pipeline.read_review(review)) == len(rows_after_first)  # not duplicated


def test_scan_checkpoint_appends_a_new_file_on_resume(tmp_path: Path) -> None:
    _make(tmp_path, "a.mp3", artist="Radiohead", title="Karma Police (Remastered)")
    review = str(tmp_path / "r.csv")
    pipeline.scan(str(tmp_path), Providers(), checkpoint=review)
    n1 = len(pipeline.read_review(review))
    _make(tmp_path, "b.mp3", artist="Billie Eilish", title="Bad Guy (Official Video)")  # added after the kill
    new = pipeline.scan(str(tmp_path), Providers(), checkpoint=review)
    assert new and len(pipeline.read_review(review)) > n1  # only b processed, its rows appended


def test_scan_continues_past_a_failing_fixer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make(tmp_path, "a.mp3", artist="Radiohead", title="Karma Police (Remastered)")

    def boom(track: object, providers: object) -> list[Proposal]:
        raise ValueError("fixer blew up")

    monkeypatch.setitem(FIXERS, "boom", boom)
    proposals = pipeline.scan(str(tmp_path), Providers())
    assert any(p.fixer == "title_junk" for p in proposals)  # other fixers still ran


def test_dedup_prefers_higher_confidence() -> None:
    low = _p("x.mp3", "title", "A", Confidence.LOW, "title_junk")
    high = _p("x.mp3", "title", "B", Confidence.HIGH, "flip")
    (kept,) = pipeline.dedup([low, high])
    assert kept.proposed == "B"


def test_dedup_breaks_ties_by_fixer_priority() -> None:
    junk = _p("x.mp3", "title", "J", Confidence.HIGH, "title_junk")
    flip = _p("x.mp3", "title", "F", Confidence.HIGH, "flip")
    (kept,) = pipeline.dedup([junk, flip])
    assert kept.fixer == "flip"  # flip outranks title_junk


def test_dedup_drops_noop_after_dash_normalize() -> None:
    # proposed equals current once the dash is normalized -> dropped
    p = Proposal("x.mp3", "title", "A - B", "A – B", Confidence.HIGH, Evidence("f", 100, "r"), "f")
    # current already ASCII, proposed normalizes to the same -> no real change
    assert pipeline.dedup([p]) == []


def test_dedup_canonicalizes_any_fixers_output() -> None:
    # a merged_field-style proposal that still holds a fullwidth char comes out ASCII
    p = Proposal(
        "x.mp3",
        "title",
        "X - ＂Jolene＂",
        "X - ＂Jolene＂ ",
        Confidence.HIGH,
        Evidence("merged_field", 100, "r"),
        "merged_field",
    )
    (kept,) = pipeline.dedup([p])
    assert kept.proposed == 'X - "Jolene"'


def test_review_round_trip(tmp_path: Path) -> None:
    p = _p("x.mp3", "artist", "New", Confidence.HIGH, "multi_artist")
    csv_path = str(tmp_path / "r.csv")
    pipeline.write_review([p], csv_path)
    rows = pipeline.read_review(csv_path)
    assert rows[0].proposed == "New" and rows[0].apply == "apply"


def test_check_scrobble_coverage_downgrades_a_title_lastfm_does_not_know(tmp_path: Path) -> None:
    from tagistry.providers import Providers
    from tagistry.providers.lastfm import LastFm

    path = _make(tmp_path, "a.mp3", artist="Coldplay", title="Clocks")
    review = str(tmp_path / "r.csv")
    _write_rows(review, ["apply", "canonicalize", "REVIEW", path, "title", "Clocks", "Clocks (radio edit)", "fp"])
    lf = LastFm("k", lambda url: {"error": 6, "message": "Track not found"})  # last.fm knows nothing
    counts = pipeline.check_scrobble_coverage(review, Providers(lastfm=lf))
    assert counts == {"checked": 1, "downgraded": 1}
    row = pipeline.read_review(review)[0]
    assert row.apply == "skip" and "last.fm" in row.evidence


def test_check_scrobble_coverage_keeps_a_title_lastfm_knows(tmp_path: Path) -> None:
    from tagistry.providers import Providers
    from tagistry.providers.lastfm import LastFm

    path = _make(tmp_path, "a.mp3", artist="Coldplay", title="Clocks")
    review = str(tmp_path / "r.csv")
    _write_rows(review, ["apply", "canonicalize", "REVIEW", path, "title", "Clocks", "Clocks (radio edit)", "fp"])
    lf = LastFm("k", lambda url: {"track": {"name": "Clocks (radio edit)", "artist": {"name": "Coldplay"}}})
    counts = pipeline.check_scrobble_coverage(review, Providers(lastfm=lf))
    assert counts == {"checked": 1, "downgraded": 0}
    assert pipeline.read_review(review)[0].apply == "apply"  # known -> kept


def test_check_scrobble_coverage_is_a_noop_without_the_api(tmp_path: Path) -> None:
    from tagistry.providers import Providers

    review = str(tmp_path / "r.csv")
    _write_rows(review, ["apply", "canonicalize", "REVIEW", "a.mp3", "title", "X", "Y", "fp"])
    # keyless (LastFmPage) or None -> the track-level gate cannot run, CSV untouched
    counts = pipeline.check_scrobble_coverage(review, Providers(lastfm=None))
    assert counts == {"checked": 0, "downgraded": 0}
    assert pipeline.read_review(review)[0].apply == "apply"


def test_check_scrobble_coverage_leaves_an_unreadable_file_alone(tmp_path: Path) -> None:
    from tagistry.providers import Providers
    from tagistry.providers.lastfm import LastFm

    review = str(tmp_path / "r.csv")
    _write_rows(review, ["apply", "canonicalize", "REVIEW", str(tmp_path / "gone.mp3"), "title", "A", "B", "fp"])
    lf = LastFm("k", lambda url: {"error": 6})  # would say unknown, but the file can't be read
    counts = pipeline.check_scrobble_coverage(review, Providers(lastfm=lf))
    assert counts == {"checked": 0, "downgraded": 0}  # unreadable -> can't verify -> not downgraded
    assert pipeline.read_review(review)[0].apply == "apply"  # untouched


def test_check_scrobble_coverage_checks_an_artist_change(tmp_path: Path) -> None:
    from tagistry.providers import Providers
    from tagistry.providers.lastfm import LastFm

    path = _make(tmp_path, "a.mp3", artist="Coldplay", title="Clocks")
    review = str(tmp_path / "r.csv")
    # an artist change: the gate checks (proposed artist, CURRENT title) -> reads the file for the title
    _write_rows(review, ["apply", "scrobble_name", "REVIEW", path, "artist", "Coldplay", "Cold Play", "fp"])
    lf = LastFm("k", lambda url: {"error": 6})  # last.fm doesn't know ('Cold Play', 'Clocks')
    counts = pipeline.check_scrobble_coverage(review, Providers(lastfm=lf))
    assert counts == {"checked": 1, "downgraded": 1}
    assert pipeline.read_review(review)[0].apply == "skip"


def test_check_scrobble_coverage_counts_only_this_runs_downgrades(tmp_path: Path) -> None:
    from tagistry.providers import Providers
    from tagistry.providers.lastfm import LastFm

    fresh = _make(tmp_path, "a.mp3", artist="X", title="Fresh")
    review = str(tmp_path / "r.csv")
    ev = "last.fm does not know this artist/title -- would orphan scrobbles"
    _write_rows(
        review,
        ["skip", "canonicalize", "REVIEW", "old.mp3", "title", "P", "Q", ev],  # a PRIOR run's downgrade
        ["apply", "canonicalize", "REVIEW", fresh, "title", "Fresh", "Fresh2", "fp"],  # a fresh apply-orphan
    )
    lf = LastFm("k", lambda url: {"error": 6})
    counts = pipeline.check_scrobble_coverage(review, Providers(lastfm=lf))
    assert counts["downgraded"] == 1  # only the fresh flip, NOT the pre-existing sentinel row


def test_scan_checkpoint_marks_clean_files_processed_so_resume_skips_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = _make(tmp_path, "clean.mp3", artist="U2", title="One")  # no fixer fires -> no proposals
    review = str(tmp_path / "r.csv")
    pipeline.scan(str(tmp_path), Providers(), checkpoint=review)

    marker = review + ".processed"
    assert Path(marker).exists() and clean in Path(marker).read_text(encoding="utf-8")  # recorded despite 0 rows

    calls: list[str] = []
    real = FIXERS["title_junk"]

    def spy(track: object, providers: object) -> list[Proposal]:
        calls.append(track.path)  # type: ignore[attr-defined]
        return real(track, providers)  # type: ignore[arg-type]

    monkeypatch.setitem(FIXERS, "title_junk", spy)
    pipeline.scan(str(tmp_path), Providers(), checkpoint=review)  # resume
    assert clean not in calls  # the clean file was skipped, not re-scanned


def test_read_review_rejects_a_missing_column(tmp_path: Path) -> None:
    # A dropped/renamed required column must fail loudly at read, not yield '' silently at apply time.
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "apply,fixer,confidence,path,field,current,evidence\napply,x,HIGH,a.mp3,title,A,e\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing column"):
        pipeline.read_review(str(bad))


def test_read_review_allows_extra_columns(tmp_path: Path) -> None:
    # an extra user-annotation column is fine -- only the required set is enforced
    good = tmp_path / "good.csv"
    header = ",".join([*pipeline.REVIEW_HEADER, "note"])
    good.write_text(f"{header}\napply,title_junk,HIGH,a.mp3,title,A,B,ev,mine\n", encoding="utf-8")
    rows = pipeline.read_review(str(good))
    assert rows[0].proposed == "B" and rows[0].is_apply


def test_status_and_undo_on_empty_log(tmp_path: Path) -> None:
    log = str(tmp_path / "none.jsonl")
    assert pipeline.status(log) == {"applied_changes": 0, "last": None}
    assert pipeline.undo(log, 3).applied == 0


def test_read_tracks_logs_and_skips_an_unreadable_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    _make(tmp_path, "good.mp3", title="Keep")
    (tmp_path / "bad.mp3").write_bytes(b"not audio at all")  # a corrupt/mislabeled file
    with caplog.at_level(logging.WARNING):
        tracks = pipeline.read_tracks(str(tmp_path))
    assert len(tracks) == 1  # the good file read; the bad one skipped, not crashed
    assert "bad.mp3" in caplog.text and "skipped 1" in caplog.text  # the miss is visible, not silent


def test_clean_temp_files_removes_only_orphaned_temps(tmp_path: Path) -> None:
    real = _make(tmp_path, "song.mp3", title="Keep")
    (tmp_path / "song.mp3.tagistry.deadbeef.tmp").write_bytes(b"orphan")
    (tmp_path / "other.mp3.tagistry.cafe1234.tmp").write_bytes(b"orphan")
    deleted, locked = pipeline.clean_temp_files(str(tmp_path))
    assert deleted == 2 and not locked
    assert Path(real).exists()  # the real file is untouched
    assert not list(tmp_path.glob("*.tmp"))  # temps gone


def test_adjudicate_decides_review_rows_by_policy(tmp_path: Path) -> None:
    review = str(tmp_path / "r.csv")
    _write_rows(
        review,
        ["skip", "canonicalize", "REVIEW", "a.mp3", "artist", "Beyonce", "Beyoncé", "fp"],  # accent add
        ["skip", "canonicalize", "REVIEW", "b.mp3", "title", "Song (Live)", "Song", "fp"],  # drops context
        ["skip", "canonicalize", "REVIEW", "c.mp3", "artist", "Jay-Z", "Jay-Z & Kanye West", "fp"],  # co-lead
        ["apply", "title_junk", "HIGH", "d.mp3", "title", "X (Remastered)", "X", "strip"],  # HIGH untouched
    )
    counts = pipeline.adjudicate(review)
    assert counts == {"apply": 2, "flag": 1, "reject": 0}
    decisions = {r.path: r.apply for r in pipeline.read_review(review)}
    assert decisions["a.mp3"] == "apply" and decisions["c.mp3"] == "apply"  # accent + co-lead applied
    assert decisions["b.mp3"] == "skip"  # context-drop flagged, not applied
    assert decisions["d.mp3"] == "apply"  # pre-existing HIGH decision untouched


def test_markers_selects_only_version_marker_restores(tmp_path: Path) -> None:
    scan = str(tmp_path / "scan.csv")
    out = str(tmp_path / "markers.csv")
    _write_rows(
        scan,
        ["skip", "canonicalize", "REVIEW", "a.mp3", "title", "ABC", "ABC (The Reflex Revision)", "fp"],  # restore
        ["skip", "canonicalize", "REVIEW", "b.mp3", "title", "Song", "Different Song", "fp"],  # plain rewrite
        ["skip", "canonicalize", "REVIEW", "c.mp3", "artist", "X", "X (Remix)", "fp"],  # marker but artist field
        ["skip", "title_junk", "REVIEW", "d.mp3", "title", "Y", "Y (Live)", "strip"],  # not canonicalize
        ["skip", "canonicalize", "REVIEW", "e.mp3", "title", "Z (Live)", "Z", "fp"],  # drops a marker
        ["skip", "canonicalize", "REVIEW", "f.mp3", "title", "", "Some Song (Remix)", "fp"],  # blank current
    )
    n = pipeline.markers(scan, out)
    kept = pipeline.read_review(out)
    assert n == 1
    assert [r.path for r in kept] == ["a.mp3"]  # blank-current row f.mp3 excluded (no marker to restore)


def test_direction_digest_surfaces_apply_decisions(tmp_path: Path) -> None:
    review = str(tmp_path / "r.csv")
    _write_rows(
        review,
        ["apply", "title_junk", "HIGH", "a.mp3", "title", "X (Remastered)", "X", "strip remaster"],
        ["skip", "canonicalize", "REVIEW", "b.mp3", "title", "Y", "Y (Live)", "fp"],
    )
    digest = pipeline.direction_digest(review)  # only_apply defaults True
    assert "[apply] title_junk title:" in digest and "-> 'X'" in digest and "[reversible]" in digest
    assert "canonicalize" not in digest  # the skip row is excluded from the apply digest
    assert "canonicalize" in pipeline.direction_digest(review, only_apply=False)  # all rows when asked


def _write_rows(csv_path: str, *rows: list[str]) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(pipeline.REVIEW_HEADER)
        for r in rows:
            w.writerow(r)


def test_flip_half_swap_is_blocked(tmp_path: Path) -> None:
    path = _make(tmp_path, "s.mp3", artist="45", title="Shinedown")
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    # user kept only the artist side of a flip pair -> would half-swap
    _write_rows(
        review,
        ["apply", "flip", "HIGH", path, "artist", "45", "Shinedown", "MB"],
        ["skip", "flip", "HIGH", path, "title", "Shinedown", "45", "MB"],
    )
    result = pipeline.apply(review, log)
    assert result.applied == 0 and result.errors  # blocked, not half-applied
    assert MediaFile(path).artist == "45" and MediaFile(path).title == "Shinedown"


def test_feat_to_title_half_apply_is_blocked(tmp_path: Path) -> None:
    path = _make(tmp_path, "s.mp3", artist="The Weeknd feat. Ariana Grande", title="Save Your Tears")
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    _write_rows(
        review,
        ["apply", "feat_to_title", "HIGH", path, "artist", "The Weeknd feat. Ariana Grande", "The Weeknd", "ev"],
        [
            "skip",
            "feat_to_title",
            "HIGH",
            path,
            "title",
            "Save Your Tears",
            "Save Your Tears (feat. Ariana Grande)",
            "ev",
        ],
    )
    result = pipeline.apply(review, log)
    assert result.applied == 0 and result.errors  # feat would be dropped -> whole group skipped
    assert MediaFile(path).artist == "The Weeknd feat. Ariana Grande"  # untouched


def test_flip_both_sides_kept_applies(tmp_path: Path) -> None:
    path = _make(tmp_path, "s.mp3", artist="45", title="Shinedown")
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    _write_rows(
        review,
        ["apply", "flip", "HIGH", path, "artist", "45", "Shinedown", "MB"],
        ["apply", "flip", "HIGH", path, "title", "Shinedown", "45", "MB"],
    )
    result = pipeline.apply(review, log)
    assert result.applied == 2 and not result.errors
    assert MediaFile(path).artist == "Shinedown" and MediaFile(path).title == "45"


def test_write_review_decision_maps_confidence(tmp_path: Path) -> None:
    props = [
        _p("a.mp3", "title", "x", Confidence.HIGH, "title_junk"),
        _p("b.mp3", "artist", "y", Confidence.REVIEW, "multi_artist"),
        _p("c.mp3", "title", "z", Confidence.LOW, "flip"),
    ]
    csv_path = str(tmp_path / "r.csv")
    pipeline.write_review(props, csv_path)
    decision = {r.path: r.apply for r in pipeline.read_review(csv_path)}
    assert decision == {"a.mp3": "apply", "b.mp3": "skip", "c.mp3": "skip"}


def test_review_csv_quotes_spreadsheet_formulas(tmp_path: Path) -> None:
    # The README's workflow opens this file in a spreadsheet, where a leading '=' is a formula
    review = str(tmp_path / "r.csv")
    payload = "=cmd|'/c calc'!A1"
    pipeline.write_review(
        [Proposal("a.mp3", "title", payload, "Clean", Confidence.REVIEW, Evidence("f", 100, "r"), "title_junk")],
        review,
    )
    raw = Path(review).read_text(encoding="utf-8")
    assert "," + payload not in raw and "'" + payload in raw  # neutralised on disk
    assert pipeline.read_review(review)[0].current == payload  # round-trips exactly


def test_review_csv_keeps_a_real_leading_apostrophe(tmp_path: Path) -> None:
    review = str(tmp_path / "r.csv")
    pipeline.write_review(
        [Proposal("a.mp3", "title", "'Round Midnight", "X", Confidence.REVIEW, Evidence("f", 1, "r"), "title_junk")],
        review,
    )
    assert pipeline.read_review(review)[0].current == "'Round Midnight"


def test_apply_refuses_a_field_edited_since_the_scan(tmp_path: Path) -> None:
    # Writing the staged proposal over a hand-edit made after the scan would destroy that edit
    path = _make(tmp_path, "a.mp3", title="Old A")
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    _write_rows(review, ["apply", "title_junk", "HIGH", path, "title", "Old A", "New A", "x"])
    tagio.write(path, {"title": "My Own Correction"})

    result = pipeline.apply(review, log)
    assert result.applied == 0 and result.errors
    assert MediaFile(path).title == "My Own Correction"


def test_apply_still_skips_a_field_already_at_the_proposed_value(tmp_path: Path) -> None:
    path = _make(tmp_path, "a.mp3", title="New A")  # a re-run after the change already landed
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    _write_rows(review, ["apply", "title_junk", "HIGH", path, "title", "Old A", "New A", "x"])

    result = pipeline.apply(review, log)
    assert result.applied == 0 and not result.errors  # idempotent, not a conflict


def test_apply_continues_past_a_bad_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _make(tmp_path, "a.mp3", title="Old A")
    b = _make(tmp_path, "b.mp3", title="Old B")
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    _write_rows(
        review,
        ["apply", "title_junk", "HIGH", a, "title", "Old A", "New A", "x"],
        ["apply", "title_junk", "HIGH", b, "title", "Old B", "New B", "x"],
    )
    real = tagio.write

    def flaky(path: str, changes: dict[str, str]) -> None:
        if path == a:
            raise ValueError("can't sync to MPEG frame")  # a mislabeled/corrupt file
        real(path, changes)

    monkeypatch.setattr(tagio, "write", flaky)
    result = pipeline.apply(review, log)
    assert result.applied == 1 and result.errors  # b applied, a reported, batch not aborted
    assert MediaFile(b).title == "New B"


def test_apply_reports_a_log_write_failure_but_keeps_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tags wrote but the undo-log append failed: count it, surface it, and never abort the whole batch.
    a = _make(tmp_path, "a.mp3", title="Old A")
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")
    _write_rows(review, ["apply", "title_junk", "HIGH", a, "title", "Old A", "New A", "x"])

    def boom(self: object, path: str, old: dict[str, str], new: dict[str, str]) -> None:
        raise OSError("disk full while appending to the undo log")

    monkeypatch.setattr(changelog.LogSession, "tag_changes", boom)
    result = pipeline.apply(review, log)
    assert result.applied == 1
    assert any("NOT logged" in e for e in result.errors)
    assert MediaFile(a).title == "New A"  # the tag change is really on disk


def test_undo_keeps_entry_that_fails_to_revert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _make(tmp_path, "a.mp3", title="A2")
    b = _make(tmp_path, "b.mp3", title="B2")
    log = str(tmp_path / "c.jsonl")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1, "path": a, "field": "title", "old": "A1", "new": "A2"}) + "\n")
        fh.write(json.dumps({"ts": 2, "path": b, "field": "title", "old": "B1", "new": "B2"}) + "\n")

    real = tagio.write

    def flaky(path: str, changes: dict[str, str]) -> None:
        if path == b:
            raise FileLockedError(b)
        real(path, changes)

    monkeypatch.setattr(tagio, "write", flaky)
    result = pipeline.undo(log, 2)
    assert result.applied == 1 and result.locked == [b]


def test_undo_and_status_tolerate_a_torn_log_line(tmp_path: Path) -> None:
    # a crash mid-append can leave a half-written last line; it must not brick undo/status
    a = _make(tmp_path, "a.mp3", title="A2")
    log = str(tmp_path / "c.jsonl")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1, "path": a, "field": "title", "old": "A1", "new": "A2"}) + "\n")
        fh.write('{"ts": 2, "path": "b.mp3", "field": "titl')  # torn line, no newline
    assert pipeline.status(log)["applied_changes"] == 1  # counts only the valid entry, no crash
    result = pipeline.undo(log, 1)
    assert result.applied == 1 and MediaFile(a).title == "A1"  # reverted the good one


def test_review_csv_carries_file_identity_context(tmp_path: Path) -> None:
    # A row records the file's artist+title, not just the changed field, so it is reviewable on its own.
    track = Track(path="x.mp3", ext="mp3", tags={"artist": "David Guetta & Chris Willis", "title": "Gettin' Over You"})
    p = make_proposal(
        track, "artist", "David Guetta & Chris Willis", "David Guetta", Confidence.REVIEW, "multi_artist", 100, "MB"
    )
    assert p.file_artist == "David Guetta & Chris Willis" and p.file_title == "Gettin' Over You"
    csv_path = str(tmp_path / "r.csv")
    pipeline.write_review([p], csv_path)
    row = pipeline.read_review(csv_path)[0]
    assert row.file_artist == "David Guetta & Chris Willis"  # the co-artist context reached the CSV
    assert row.file_title == "Gettin' Over You"  # the song -- needed to judge the split -- is present


def test_read_review_loads_old_csv_without_context_columns(tmp_path: Path) -> None:
    # an OLD review/scan CSV (written before the file_* columns) must still load -- context empty.
    old = tmp_path / "old.csv"
    old.write_text(
        "apply,fixer,confidence,path,field,current,proposed,evidence\nskip,flip,REVIEW,a.mp3,title,A,B,ev\n",
        encoding="utf-8",
    )
    rows = pipeline.read_review(str(old))
    assert len(rows) == 1 and rows[0].proposed == "B"
    assert rows[0].file_artist == "" and rows[0].file_title == ""  # missing context -> blank, no crash
