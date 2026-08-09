"""Two-fingerprinter agreement: classify verdicts + shazam_filter downgrades non-AGREE rows."""

from __future__ import annotations

import csv
from pathlib import Path

from tagistry import gates, pipeline, shazam

# --- classify (the canonical agreement logic, mirrored by scripts/shazam_verify.py) ---


def test_classify_agree_when_shazam_matches_proposed() -> None:
    assert shazam.classify("Say It", "Say It (Illenium Remix)", "Say It (Illenium Remix)") == shazam.AGREE


def test_classify_agree_by_base_and_marker_parity() -> None:
    # same base title, both carry a marker -> agree even when not string-similar overall
    assert shazam.classify("Song", "Song (Live)", "Song (Live at Wembley)") == shazam.AGREE


def test_classify_says_plain_when_shazam_matches_current() -> None:
    assert shazam.classify("Clocks", "Clocks (radio edit)", "Clocks") == shazam.SAYS_PLAIN
    assert shazam.classify("Samba Pa Ti", "Maria Maria", "Samba Pa Ti") == shazam.SAYS_PLAIN


def test_classify_says_plain_via_base_equality_not_similarity() -> None:
    # Too different for the sim>0.85 arm, but the BASE titles match -> SAYS_PLAIN (marker unconfirmed).
    assert shazam.classify("Song", "Song (Live)", "Song (At Madison Square Garden)") == shazam.SAYS_PLAIN


def test_classify_different_when_shazam_names_another_song() -> None:
    assert shazam.classify("Echoes", "Point Pleasant", "Some Other Song") == shazam.DIFFERENT


def test_classify_no_match_on_empty_shazam() -> None:
    assert shazam.classify("X", "X (remix)", "") == shazam.NO_MATCH


# --- shazam_filter ----------------------------------------------------------


def _review(path: str, *rows: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(pipeline.REVIEW_HEADER)
        for r in rows:
            w.writerow(r)


def _verdicts(path: str, mapping: dict[str, str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["path", "verdict", "shazam_title"])
        w.writeheader()
        for p, v in mapping.items():
            w.writerow({"path": p, "verdict": v, "shazam_title": ""})


def test_shazam_filter_keeps_agree_downgrades_the_rest(tmp_path: Path) -> None:
    review = str(tmp_path / "r.csv")
    verdicts = str(tmp_path / "v.csv")
    _review(
        review,
        ["apply", "blank_id", "HIGH", "a.mp3", "title", "", "Real Song", "AcoustID 0.95"],  # agree -> keep
        ["skip", "canonicalize", "REVIEW", "b.mp3", "title", "Song", "Song (Live)", "fp"],  # says_plain -> down
        ["apply", "blank_id", "HIGH", "c.mp3", "artist", "", "Someone", "fp"],  # no verdict -> down
        ["apply", "title_junk", "HIGH", "d.mp3", "title", "X (Remastered)", "X", "strip"],  # not fingerprint
    )
    _verdicts(verdicts, {"a.mp3": "AGREE", "b.mp3": "SAYS_PLAIN"})
    counts = pipeline.shazam_filter(review, verdicts)
    rows = {r.path: r for r in pipeline.read_review(review)}

    assert counts == {"agree": 1, "downgraded": 2, "untouched": 1}
    assert rows["a.mp3"].apply == "apply" and rows["a.mp3"].confidence == "HIGH"  # agreed fingerprint kept HIGH
    assert rows["b.mp3"].apply == "skip" and "SAYS_PLAIN" in rows["b.mp3"].evidence
    # a fingerprint HIGH with no 2nd-fingerprinter verdict must not auto-apply -> skip + REVIEW
    assert rows["c.mp3"].apply == "skip" and rows["c.mp3"].confidence == "REVIEW"
    assert rows["d.mp3"].apply == "apply" and rows["d.mp3"].confidence == "HIGH"  # non-fingerprint untouched


def _verdicts_with_artist(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["path", "verdict", "shazam_title", "shazam_artist"])
        w.writeheader()
        w.writerows(rows)


def test_shazam_filter_downgrades_artist_rewrite_shazam_did_not_hear(tmp_path: Path) -> None:
    # AGREE compares TITLES: an artist rewrite Shazam did not hear still downgrades.
    review = str(tmp_path / "r.csv")
    verdicts = str(tmp_path / "v.csv")
    _review(
        review,
        ["skip", "canonicalize", "REVIEW", "a.mp3", "artist", "Ladyhawke", "B.o.B", "fp"],  # wrong artist
        ["skip", "canonicalize", "REVIEW", "a.mp3", "title", "Magic", "Magic (feat. X)", "fp"],  # title agreed
        ["skip", "canonicalize", "REVIEW", "b.mp3", "artist", "Fortress", "Pinback", "fp"],  # right artist
    )
    _verdicts_with_artist(
        verdicts,
        [
            {"path": "a.mp3", "verdict": "AGREE", "shazam_title": "Magic", "shazam_artist": "Ladyhawke"},
            {"path": "b.mp3", "verdict": "AGREE", "shazam_title": "Fortress", "shazam_artist": "Pinback"},
        ],
    )
    pipeline.shazam_filter(review, verdicts)
    rows = {(r.path, r.field): r for r in pipeline.read_review(review)}
    # 'B.o.B' is not what Shazam heard ('Ladyhawke') -> the artist rewrite is downgraded despite AGREE
    assert rows[("a.mp3", "artist")].apply == "skip" and "does not confirm" in rows[("a.mp3", "artist")].evidence
    assert rows[("a.mp3", "title")].evidence == "fp"  # the agreed TITLE row is kept, untouched
    assert rows[("b.mp3", "artist")].evidence == "fp"  # Shazam heard 'Pinback' -> the rewrite is confirmed


def test_shazam_filter_writes_to_out_csv(tmp_path: Path) -> None:
    review = str(tmp_path / "r.csv")
    verdicts = str(tmp_path / "v.csv")
    out = str(tmp_path / "out.csv")
    _review(review, ["apply", "blank_id", "HIGH", "a.mp3", "title", "", "Guess", "fp"])
    _verdicts(verdicts, {"a.mp3": "DIFFERENT"})
    pipeline.shazam_filter(review, verdicts, out)
    assert pipeline.read_review(out)[0].apply == "skip"  # downgraded in the copy
    assert pipeline.read_review(review)[0].apply == "apply"  # original untouched


def test_artist_corroboration_rejects_an_unrelated_shorter_name() -> None:
    # Substring containment corroborated unrelated acts ('Sia' in 'Siames'); last gate before an artist rewrite.
    for proposed, heard in [("Sia", "Siames"), ("Ash", "Ashanti"), ("Air", "Airbourne"), ("Nas", "Nasty C")]:
        assert not gates._artist_corroborated(proposed, heard), f"{proposed} vs {heard}"


def test_artist_corroboration_still_accepts_a_co_lead_credit() -> None:
    assert gates._artist_corroborated("Sia", "Sia & Sean Paul")
    assert gates._artist_corroborated("Beyonce", "Beyoncé")
