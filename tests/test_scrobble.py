"""The pure most-scrobbled-name decision. No I/O."""

from __future__ import annotations

from tagistry.scrobble import pick_scrobble_name


def test_switches_on_a_clear_win() -> None:
    # Hebrew name has far more scrobbles than the Latin tag -> switch
    c = pick_scrobble_name("Knesiyat Hasekhel", {"Knesiyat Hasekhel": 97, "כנסיית השכל": 13900})
    assert c.changed and c.name == "כנסיית השכל"


def test_current_unknown_but_alias_known_switches() -> None:
    c = pick_scrobble_name("Some Tag", {"Real Name": 5000})  # current absent from last.fm
    assert c.changed and c.name == "Real Name"


def test_tie_keeps_current() -> None:
    # same canonical page returns equal counts -> no gratuitous rewrite
    c = pick_scrobble_name("Metropolin", {"Metropolin": 11100, "מטרופולין": 11100})
    assert not c.changed and c.name == "Metropolin"


def test_small_margin_keeps_current() -> None:
    # 8880 vs 3030: current already bigger -> keep (it is the winner)
    c = pick_scrobble_name("Hanan Ben Ari", {"Hanan Ben Ari": 8880, "חנן בן ארי": 3030})
    assert not c.changed and c.name == "Hanan Ben Ari"


def test_within_margin_keeps_current() -> None:
    # alias slightly bigger but under 1.25x -> keep current, don't switch
    c = pick_scrobble_name("A", {"A": 1000, "B": 1100})
    assert not c.changed and c.name == "A"


def test_over_margin_switches() -> None:
    c = pick_scrobble_name("A", {"A": 1000, "B": 1300})  # 1.3x >= 1.25x
    assert c.changed and c.name == "B"


def test_no_data_keeps_current() -> None:
    c = pick_scrobble_name("A", {})
    assert not c.changed and c.name == "A"


def test_zero_listeners_alias_keeps_current() -> None:
    # Both spellings at 0 listeners: 0 >= 0 * 1.25 must NOT switch, nothing prefers the alias.
    c = pick_scrobble_name("Spelling B", {"Spelling A": 0})
    assert not c.changed and c.name == "Spelling B"


def test_current_already_top_by_key_no_change() -> None:
    # best equals current up to case/accent match form -> keep the current written form
    c = pick_scrobble_name("tiësto", {"tiësto": 900000, "Tiesto": 900000})
    assert not c.changed and c.name == "tiësto"
