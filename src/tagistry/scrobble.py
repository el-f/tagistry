"""Pick the artist spelling that scrobbles land on: the most-scrobbled last.fm form.

An artist can be tagged under several real spellings -- a transliteration ('Tiesto' vs 'Tiësto'),
a script variant ('Din Din Aviv' vs 'דין דין אביב'), or a rename ('Kanye West' vs 'Ye'). last.fm
scrobbles match ONE spelling; the rest orphan. The right canonical tag is the spelling with the
most last.fm listeners. Identity comes from a MusicBrainz MBID (its aliases are the same artist by
construction); popularity comes from last.fm; this module is the pure decision between them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .text import key


@dataclass(frozen=True, slots=True)
class NameChoice:
    name: str  # the spelling to use
    reason: str
    changed: bool  # True when name differs from the current tag


def pick_scrobble_name(current: str, counts: dict[str, int], margin: float = 1.25) -> NameChoice:
    """Choose the artist spelling with the most last.fm listeners, switching from `current` only on
    a CLEAR win. `counts` maps each same-artist spelling (current + MusicBrainz aliases) to its
    last.fm listener count; a spelling last.fm does not know is simply absent. Pure -- no I/O.

    Rules: switch when the best spelling has >= `margin`x the current's listeners, OR the current is
    unknown to last.fm while a candidate is known. A tie (same canonical page, equal counts) or a
    small margin keeps the current tag -- never a gratuitous rewrite.
    """
    if not counts:
        return NameChoice(current, "no last.fm data for any spelling", False)
    best = max(counts, key=lambda n: (counts[n], key(n) == key(current)))  # ties prefer keeping current
    best_n = counts[best]
    cur_n = counts.get(current, 0)
    if key(best) == key(current):
        return NameChoice(current, f"current is already the most-scrobbled spelling ({cur_n} listeners)", False)
    if cur_n == 0 and best_n > 0:
        return NameChoice(best, f"current has no last.fm page; '{best}' has {best_n} listeners", True)
    # `0 >= 0 * margin` is vacuously true, so without best_n > 0 a 0-vs-0 pair would still switch
    if best_n > 0 and best_n >= cur_n * margin:
        return NameChoice(best, f"'{best}' {best_n} listeners vs current {cur_n} (>= {margin:g}x)", True)
    return NameChoice(current, f"current {cur_n} close to best {best_n} (< {margin:g}x) -- keep", False)
