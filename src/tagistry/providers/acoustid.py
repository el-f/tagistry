"""AcoustID fingerprint provider, via pyacoustid + fpcalc.

Fingerprints a file, returns the best AcoustID match above the score floor.
AcoustID gives an MBID + basic metadata; a follow-up MB lookup can enrich it.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import acoustid

from ..text import key

_KEY_FILE = Path.home() / ".acoustid_key"
_MIN_SCORE = 0.85
_MIN_INTERVAL = 0.34  # AcoustID allows 3 req/s
_TIE_BAND = 0.02  # candidates within this of the top score are a tie
# pyacoustid looks up over urllib, which has no per-call timeout -- a hung request blocks the scan
_LOOKUP_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class AcoustIDMatch:
    score: float
    artist: str
    title: str
    recording_id: str


class NoFpcalcError(RuntimeError):
    """fpcalc binary not found on PATH or at the known fallback."""


class AcoustID:
    def __init__(self, api_key: str | None = None, min_score: float = _MIN_SCORE) -> None:
        self._key = api_key or self._load_key()
        self._min = min_score
        self._last = 0.0
        self._cache: dict[str, AcoustIDMatch | None] = {}  # memoize per path within a scan
        self._ensure_fpcalc()

    def _throttle(self) -> None:
        wait = self._last + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @staticmethod
    def _load_key() -> str:
        if not _KEY_FILE.exists():
            raise RuntimeError(f"AcoustID key not found at {_KEY_FILE}")
        return _KEY_FILE.read_text(encoding="utf-8").strip()

    @staticmethod
    def _ensure_fpcalc() -> None:
        if os.environ.get("FPCALC"):
            return
        override = os.environ.get("TAGISTRY_FPCALC")
        if override and Path(override).exists():
            os.environ["FPCALC"] = override
            return
        # Otherwise pyacoustid finds fpcalc on PATH; a missing binary surfaces at match time.

    def identify(self, path: str) -> AcoustIDMatch | None:
        """Confident match above the score floor, or None. Memoized per path: blank_id,
        canonicalize, and year_fill all fingerprint the same file in one scan, so the fpcalc +
        AcoustID lookup runs once, not three times."""
        if path not in self._cache:
            self._cache[path] = self._lookup(path)
        return self._cache[path]

    def _lookup(self, path: str) -> AcoustIDMatch | None:
        """Rate-limited fingerprint lookup; a transient web error backs off once then gives up
        (returns None) so a batch run never aborts on it.

        An AcoustID lookup can return several different recordings at the same top score: a
        different song from the same album, or a marker variant of one song (plain vs (radio
        edit)/(instrumental)/(5.1 mix)). Picking one is a coin flip that silently rewrites a
        correct tag, so ANY tie between distinct titles is rejected (None) rather than resolved."""
        prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_LOOKUP_TIMEOUT)  # bound urllib inside pyacoustid; restored below
        try:
            for attempt in range(2):
                self._throttle()
                try:
                    cands = [
                        (float(score), str(rid), str(title), str(artist))
                        for score, rid, title, artist in acoustid.match(self._key, path)
                        if artist and title
                    ]
                    return self._resolve(cands)
                except acoustid.NoBackendError as exc:
                    raise NoFpcalcError("fpcalc/chromaprint not available") from exc
                except acoustid.FingerprintGenerationError:
                    return None  # unreadable/corrupt audio — not identifiable, don't retry
                except acoustid.WebServiceError:
                    if attempt == 0:
                        time.sleep(2.0)  # rate-limited or transient network — back off, retry once
            return None
        finally:
            socket.setdefaulttimeout(prev)

    def _resolve(self, cands: list[tuple[float, str, str, str]]) -> AcoustIDMatch | None:
        """Pick the confident match from the raw candidate list, or None if ambiguous.

        Candidates within _TIE_BAND of the top score are the tie. If they carry two or more
        distinct titles the fingerprint can't say which is the file -> None. AcoustID lists the
        same recording under several release entries, so titles are compared on the match key
        (accent/case-folded): those collapse to one and don't count as a tie."""
        if not cands:
            return None
        top = max(c[0] for c in cands)
        near = [c for c in cands if c[0] >= top - _TIE_BAND]
        if len({key(title) for _, _, title, _ in near}) >= 2:
            return None
        score, rid, title, artist = max(near, key=lambda c: c[0])
        match = AcoustIDMatch(score, artist, title, rid)
        return match if match.score >= self._min else None
