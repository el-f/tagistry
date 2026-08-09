"""Shazam cross-check: a second, independent fingerprinter to confirm a fingerprint proposal.

AcoustID and Shazam use different fingerprints. A retitle / version-marker proposal is only
trustworthy when BOTH agree. This script Shazams each file in a review CSV and marks whether
Shazam AGREEs with the proposed title, says the CURRENT (plain) title, or names something else.

ENV: shazamio's Rust core SEGFAULTS on Python 3.14 and pydub needs `audioop` which 3.13 removed,
so run under 3.12, isolated from the 3.14 project venv:

    uv run --no-project --python 3.12 --with shazamio python scripts/shazam_verify.py \
        --review tagistry_markers_review.csv --out tagistry_shazam_verdicts.csv

Input CSV columns: at least `path`, `current`, `proposed`. Output adds shazam_title / shazam_artist
/ isrc / verdict. Throttled (unofficial API), incremental + resumable (re-run skips done rows).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import os
import sys
import warnings
from pathlib import Path

# Isolated py3.12 env (shazamio segfaults on 3.14): tagistry is not installed, so add src/ and reuse classify().
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tagistry.shazam import AGREE, DIFFERENT, NO_MATCH, SAYS_PLAIN, classify

# A cp1252 console dies on accented titles; replace-on-encode stops a cosmetic line aborting the run.
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _meta(track: dict) -> dict:
    md = {}
    for sec in track.get("sections") or []:
        for m in sec.get("metadata") or []:
            md[m.get("title", "")] = m.get("text", "")
    return {
        "shazam_title": track.get("title", ""),
        "shazam_artist": track.get("subtitle", ""),
        "isrc": track.get("isrc", ""),
        "album": md.get("Album", ""),
        "released": md.get("Released", ""),
    }


async def _run(review: str, out: str, throttle: float) -> None:
    from shazamio import Shazam  # imported here so the module loads without shazamio (self-check)

    with open(review, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    done = {}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            done = {r["path"]: r for r in csv.DictReader(fh)}
    sh = Shazam()
    results = list(done.values())
    for i, row in enumerate(rows):
        if row["path"] in done:
            continue
        rec = dict(row)
        try:
            res = await sh.recognize(row["path"])
            if res.get("matches") and res.get("track"):
                rec.update(_meta(res["track"]))
            rec["verdict"] = classify(row.get("current", ""), row.get("proposed", ""), rec.get("shazam_title", ""))
        except Exception as e:  # unofficial API -- never abort the batch on one file
            rec["verdict"] = f"ERROR: {str(e)[:50]}"
        results.append(rec)
        fields = sorted({k for r in results for k in r})
        with open(out, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(results)
        print(
            f"{i + 1}/{len(rows)} {rec['verdict'][:10]:10} "
            f"{row.get('current', '')[:24]:24} vs shazam '{rec.get('shazam_title', '')[:22]}'"
        )
        await asyncio.sleep(throttle)
    print(f"\ndone: {len(results)} verdicts -> {out}")


def _selfcheck() -> None:
    assert classify("Clocks", "Clocks (radio edit)", "Clocks") == SAYS_PLAIN
    assert classify("Say It", "Say It (Illenium remix)", "Say It (Illenium Remix)") == AGREE
    assert classify("Samba Pa Ti", "Maria Maria", "Samba Pa Ti") == SAYS_PLAIN
    assert classify("Echoes", "Point Pleasant", "Some Other Song") == DIFFERENT
    assert classify("X", "X (remix)", "") == NO_MATCH
    print("selfcheck ok")


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(description="Shazam cross-check a review CSV (run under python 3.12).")
    ap.add_argument("--review", help="input review CSV (needs path/current/proposed columns)")
    ap.add_argument("--out", help="output verdict CSV")
    ap.add_argument("--throttle", type=float, default=2.5, help="seconds between Shazam calls")
    ap.add_argument("--selfcheck", action="store_true", help="run the pure-logic self-check and exit")
    a = ap.parse_args()
    if a.selfcheck or not a.review:
        _selfcheck()
    else:
        asyncio.run(_run(a.review, a.out or "shazam_verdicts.csv", a.throttle))
