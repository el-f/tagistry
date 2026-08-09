"""Read-only library health: duplicate groups and the anomaly report."""

from __future__ import annotations

import shutil
from pathlib import Path

from mediafile import MediaFile

from tagistry import audits, planners
from tagistry.domain import Confidence, Track

FIXTURES = Path(__file__).parent / "fixtures"


def _t(path: str, length: float = 180.0, **tags: str) -> Track:
    base = {"artist": "", "title": "", "album": ""}
    base.update(tags)
    return Track(path=path, ext="mp3", tags=base, length=length)


def _song(dir_: Path, name: str, src: str = "sample.mp3", **tags: str) -> str:
    path = dir_ / name
    shutil.copy2(FIXTURES / src, path)
    mf = MediaFile(str(path))
    for k, v in tags.items():
        setattr(mf, k, v)
    mf.save()
    return str(path)


def test_find_duplicates_groups_same_artist_title(tmp_path: Path) -> None:
    _song(tmp_path, "a.mp3", artist="Muse", title="Hysteria")
    _song(tmp_path, "b.mp3", artist="muse", title="hysteria")  # same, different case
    _song(tmp_path, "c.mp3", artist="Muse", title="Starlight")  # unique
    groups = audits.find_duplicates(str(tmp_path))
    assert len(groups) == 1 and len(groups[0]) == 2


def test_find_duplicates_none_when_all_unique(tmp_path: Path) -> None:
    _song(tmp_path, "a.mp3", artist="A", title="One")
    _song(tmp_path, "b.mp3", artist="A", title="Two")
    assert audits.find_duplicates(str(tmp_path)) == []


def test_audit_flags_blank_and_artist_equals_title(tmp_path: Path) -> None:
    _song(tmp_path, "blank.mp3", artist="", title="Orphan")
    _song(tmp_path, "eq.mp3", artist="Song Name", title="Song Name")
    _song(tmp_path, "multi.mp3", artist="2Pac, Dr. Dre", title="California Love")
    _song(tmp_path, "ok.mp3", artist="U2", title="One")
    issues = {Path(p).name: msg for p, msg in audits.audit_library(str(tmp_path))}
    assert "blank artist" in issues.get("blank.mp3", "")
    assert issues.get("eq.mp3") == "artist == title"
    assert "multi-artist" in issues.get("multi.mp3", "")
    assert "ok.mp3" not in issues  # clean file has no finding


def test_audit_flags_all_three_equal(tmp_path: Path) -> None:
    _song(tmp_path, "x.mp3", artist="Same", title="Same", album="Same")
    (_, msg), *_ = audits.audit_library(str(tmp_path))
    assert msg == "artist == title == album"


def test_audit_keeps_colead_when_both_sides_known(tmp_path: Path) -> None:
    # Each artist appears twice -> known; their joined "&"/comma credit is a co-lead, not a split.
    _song(tmp_path, "w1.mp3", artist="The Weeknd", title="Blinding Lights")
    _song(tmp_path, "w2.mp3", artist="The Weeknd", title="Save Your Tears")
    _song(tmp_path, "a1.mp3", artist="Ariana Grande", title="7 rings")
    _song(tmp_path, "a2.mp3", artist="Ariana Grande", title="positions")
    _song(tmp_path, "collab.mp3", artist="The Weeknd, Ariana Grande", title="Save Your Tears (Remix)")
    issues = {Path(p).name: msg for p, msg in audits.audit_library(str(tmp_path))}
    assert "collab.mp3" not in issues  # co-lead of two known artists is not flagged


def test_audit_relabels_long_blank_mix() -> None:
    # a blank, hours-long file alone in its folder is a YouTube/OST mix, not a fixable anomaly
    issues = dict(audits.audit_tracks([_t("Mixes/2h chill mix.mp3", length=7200)]))
    assert "mix" in issues["Mixes/2h chill mix.mp3"]


def test_audit_short_blank_stays_blank() -> None:
    issues = dict(audits.audit_tracks([_t("Songs/x.mp3", length=200, title="Orphan")]))
    assert issues["Songs/x.mp3"] == "blank artist"


def test_audit_long_tagged_file_is_clean() -> None:
    # a long file WITH good tags is not a problem -- the mix relabel only touches blank files
    assert audits.audit_tracks([_t("Live/jam.mp3", length=3600, artist="Phish", title="Tweezer")]) == []


def test_audit_long_blank_but_crowded_folder_stays_blank() -> None:
    # long+blank but the folder holds many tracks -> not a single-file mix, keep the blank flag
    tracks = [_t(f"Album/{i}.mp3", length=1500, title=f"T{i}") for i in range(4)]
    issues = dict(audits.audit_tracks(tracks))
    assert issues["Album/0.mp3"] == "blank artist"


def test_plan_albumartist_fills_blank_in_single_artist_folder() -> None:
    tracks = [
        _t("Metallica - Master/1.mp3", artist="Metallica", title="Battery"),
        _t("Metallica - Master/2.mp3", artist="Metallica", title="Master of Puppets", albumartist="Metallica"),
        _t("Metallica - Master/3.mp3", artist="Metallica", title="Leper Messiah"),
    ]
    props = planners.plan_albumartist(tracks)
    assert {p.track_path for p in props} == {"Metallica - Master/1.mp3", "Metallica - Master/3.mp3"}
    assert all(p.field == "albumartist" and p.proposed == "Metallica" for p in props)
    assert all(p.confidence is Confidence.REVIEW for p in props)


def test_plan_albumartist_skips_compilation() -> None:
    tracks = [_t(f"VA - Hits/{i}.mp3", artist=a, title="x") for i, a in enumerate("ABC")]
    assert planners.plan_albumartist(tracks) == []


def test_audit_still_flags_feat_in_artist(tmp_path: Path) -> None:
    # feat is a guest, not a co-lead -> still flagged even when both are known artists
    _song(tmp_path, "d1.mp3", artist="Drake", title="One Dance")
    _song(tmp_path, "d2.mp3", artist="Drake", title="Hotline Bling")
    _song(tmp_path, "r1.mp3", artist="Rihanna", title="Work")
    _song(tmp_path, "r2.mp3", artist="Rihanna", title="Diamonds")
    _song(tmp_path, "feat.mp3", artist="Drake feat. Rihanna", title="Too Good")
    issues = {Path(p).name: msg for p, msg in audits.audit_library(str(tmp_path))}
    assert "multi-artist" in issues.get("feat.mp3", "")
