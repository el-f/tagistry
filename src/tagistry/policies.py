"""Deterministic adjudication policies for canonicalize/rewrite REVIEW rows.

The review policies as CODE, not an agent prompt, so
they are golden-tested and reproducible:
- keep proper accents: adding one is canonical (apply), stripping one is a loss (reject);
- an MB-canonical rewrite that DROPS user context (a shorter subset) -> flag for a human;
- an unverifiable version marker (remix / live / ...) -> flag;
- a verified co-lead '& X' added on the end -> apply.

adjudicate_change returns a verdict -- 'apply' (safe to write), 'flag' (needs a human), or
'reject' (keep the current value) -- plus a one-line reason. Anything it can't place is 'flag',
never a silent auto-apply.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal, get_args

from .text import has_version_marker, key, norm, subset, tokens

Verdict = Literal["apply", "flag", "reject"]
# The valid verdicts, derived from the Literal so a consumer's counter can't drift from it.
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)

_COARTIST_TAIL = re.compile(r"^(and|feat\.?|ft\.?|with|x|vs)\b", re.IGNORECASE)


def _accent_weight(s: str) -> int:
    """Count of combining marks (accents) -- more means a more accented spelling."""
    return sum(1 for c in unicodedata.normalize("NFKD", s) if unicodedata.combining(c))


def _adds_coartist(current: str, proposed: str) -> bool:
    """proposed keeps all of current and appends a co-lead WITH a name ('A' -> 'A & B',
    'A feat. B'). A dangling keyword and no name ('A and', 'A feat.') is NOT a valid add."""
    c, p = norm(current), norm(proposed)  # norm folds '&' -> 'and', accents, case
    if not c or c == p or not p.startswith(c + " "):
        return False
    tail = p[len(c) :].strip()
    m = _COARTIST_TAIL.match(tail)
    return bool(m and re.search(r"\w", tail[m.end() :]))  # a real name (a word char) must follow the keyword


def _drops_context(current: str, proposed: str) -> bool:
    """proposed's tokens are a STRICT subset of current's -- it shortened the value (dropped a
    '(Live)', a '(feat. X)', an edition). A same-token reorder is not a drop (fewer tokens only)."""
    return subset(proposed, current) and len(tokens(proposed)) < len(tokens(current))


def adjudicate_change(current: str, proposed: str) -> tuple[Verdict, str]:
    """Classify a single (current -> proposed) rewrite by the review policies."""
    if not proposed.strip():
        return "reject", "would clear the field -- keep current"
    if key(current) == key(proposed):
        # same match key: only accents / case / spacing differ
        ac, ap = _accent_weight(current), _accent_weight(proposed)
        if ap > ac:
            return "apply", "adds a proper accent (canonical)"
        if ac > ap:
            return "reject", "would strip an accent -- keep current"
        return "apply", "case/spacing normalization"
    if _adds_coartist(current, proposed):
        return "apply", "adds a co-lead artist"
    if _drops_context(current, proposed):
        return "flag", "canonical form drops user context"
    if has_version_marker(current) or has_version_marker(proposed):
        return "flag", "unverifiable version marker"
    return "flag", "unverified rewrite"
