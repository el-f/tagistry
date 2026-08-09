"""MCP adapter: tools are registered and drive the offline scan -> apply -> undo path.

Uses fastmcp's in-memory Client (no socket), so it runs under --block-network.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from mediafile import MediaFile

from tagistry.mcp_server import mcp

FIXTURES = Path(__file__).parent / "fixtures"

# asyncio's loopback self-pipe would trip --block-network; allow loopback only, real hosts stay blocked.
loopback_only = pytest.mark.block_network(allowed_hosts=["127.0.0.1", "::1", "localhost"])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@loopback_only
def test_all_tools_registered() -> None:
    async def go() -> set[str]:
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    names = _run(go())
    expected = {
        "scan",
        "list_proposals",
        "apply",
        "undo",
        "status",
        "rename",
        "coverart",
        "disambiguate",
        "adjudicate",
        "markers",
        "shazam_filter",
        "scrobble_check",
        "scrobble_names",
        "albumartist",
        "duplicates",
        "doctor",
        "review",
        "apply_renames",
        "clean",
        "plex_refresh",
    }
    assert expected <= names


@loopback_only
def test_scan_apply_undo_via_mcp(tmp_path: Path) -> None:
    song = tmp_path / "s.mp3"
    shutil.copy2(FIXTURES / "sample.mp3", song)
    mf = MediaFile(str(song))
    mf.artist, mf.title = "Radiohead", "Karma Police (Remastered)"
    mf.save()
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")

    async def go() -> dict[str, object]:
        async with Client(mcp) as c:
            await c.call_tool("scan", {"root": str(tmp_path), "review": review, "online": False})
            await c.call_tool("apply", {"review": review, "log": log, "dry_run": False})
            st = await c.call_tool("status", {"log": log})
            return dict(st.data)

    data = _run(go())
    assert MediaFile(str(song)).title == "Karma Police"  # junk stripped through MCP
    assert data["applied_changes"] == 1

    async def revert() -> None:
        async with Client(mcp) as c:
            await c.call_tool("undo", {"n": 1, "log": log})

    _run(revert())
    assert MediaFile(str(song)).title == "Karma Police (Remastered)"  # undo through MCP


@loopback_only
def test_mcp_apply_defaults_to_dry_run(tmp_path: Path) -> None:
    # an agent must not write tags unreviewed: every writing MCP tool defaults to dry_run
    song = tmp_path / "s.mp3"
    shutil.copy2(FIXTURES / "sample.mp3", song)
    mf = MediaFile(str(song))
    mf.artist, mf.title = "Radiohead", "Karma Police (Remastered)"
    mf.save()
    review, log = str(tmp_path / "r.csv"), str(tmp_path / "c.jsonl")

    async def go() -> None:
        async with Client(mcp) as c:
            await c.call_tool("scan", {"root": str(tmp_path), "review": review, "online": False})
            await c.call_tool("apply", {"review": review, "log": log})

    _run(go())
    assert MediaFile(str(song)).title == "Karma Police (Remastered)"  # untouched
    assert not Path(log).exists()  # nothing logged, because nothing was written


@loopback_only
def test_mcp_rename_defaults_to_dry_run(tmp_path: Path) -> None:
    # an agent must not move files unreviewed: the MCP rename tool defaults to dry_run
    song = tmp_path / "bad name.mp3"
    shutil.copy2(FIXTURES / "sample.mp3", song)
    mf = MediaFile(str(song))
    mf.artist, mf.title = "Muse", "Hysteria"
    mf.save()

    async def go() -> dict[str, object]:
        async with Client(mcp) as c:
            r = await c.call_tool("rename", {"root": str(tmp_path)})
            return dict(r.data)

    data = _run(go())
    assert data["dry_run"] is True
    assert song.exists()  # nothing moved
    assert not (tmp_path / "Muse - Hysteria.mp3").exists()


@loopback_only
def test_mcp_adjudicate_and_review(tmp_path: Path) -> None:
    import csv

    from tagistry import pipeline

    review = str(tmp_path / "r.csv")
    with open(review, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(pipeline.REVIEW_HEADER)
        w.writerow(["skip", "canonicalize", "REVIEW", "a.mp3", "artist", "Beyonce", "Beyoncé", "fp"])  # accent -> apply

    async def go() -> tuple[dict[str, object], dict[str, object]]:
        async with Client(mcp) as c:
            adj = await c.call_tool("adjudicate", {"review": review})
            rev = await c.call_tool("review", {"review": review})
            return dict(adj.data), dict(rev.data)

    adj, rev = _run(go())
    assert adj["apply"] == 1  # the accent-add was applied by policy
    assert rev["total"] == 1


@loopback_only
def test_mcp_rename_stage_writes_a_plan_without_moving(tmp_path: Path) -> None:
    song = tmp_path / "bad name.mp3"
    shutil.copy2(FIXTURES / "sample.mp3", song)
    mf = MediaFile(str(song))
    mf.artist, mf.title = "Muse", "Hysteria"
    mf.save()
    plan = str(tmp_path / "plan.csv")

    async def go() -> dict[str, object]:
        async with Client(mcp) as c:
            r = await c.call_tool("rename", {"root": str(tmp_path), "stage": plan})
            return dict(r.data)

    data = _run(go())
    assert data["staged"] == 1 and Path(plan).exists() and song.exists()


def _junk_lib(tmp_path: Path) -> str:
    song = tmp_path / "s.mp3"
    shutil.copy2(FIXTURES / "sample.mp3", song)
    mf = MediaFile(str(song))
    mf.artist, mf.title = "Radiohead", "Karma Police (Remastered)"  # only title_junk proposes here
    mf.save()
    return str(tmp_path)


@loopback_only
def test_scan_disambiguate_params_present_in_both_adapters() -> None:
    # Parity check: these params must stay in BOTH the CLI command and the MCP tool, or the adapters drifted.
    import inspect

    from tagistry import cli

    assert {"fixers", "discogs", "resume"} <= set(inspect.signature(cli.scan).parameters)
    assert "timeout" in set(inspect.signature(cli.disambiguate).parameters)

    async def tool_props() -> dict[str, set[str]]:
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            return {name: set(tools[name].inputSchema.get("properties", {})) for name in ("scan", "disambiguate")}

    props = _run(tool_props())
    assert {"fixers", "discogs", "resume"} <= props["scan"]  # gained on the MCP scan tool
    assert "timeout" in props["disambiguate"]  # gained on the MCP disambiguate tool


@loopback_only
def test_mcp_scan_fixers_subset(tmp_path: Path) -> None:
    # The --fixers subset flows through MCP: title_junk runs when named, and is excluded when another is.
    root = _junk_lib(tmp_path)
    r1, r2 = str(tmp_path / "r1.csv"), str(tmp_path / "r2.csv")

    async def go() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        async with Client(mcp) as c:
            await c.call_tool("scan", {"root": root, "review": r1, "online": False, "fixers": "title_junk"})
            await c.call_tool("scan", {"root": root, "review": r2, "online": False, "fixers": "normalize"})
            p1 = await c.call_tool("list_proposals", {"review": r1})
            p2 = await c.call_tool("list_proposals", {"review": r2})
            return list(p1.data), list(p2.data)

    named, other = _run(go())
    assert any(r["fixer"] == "title_junk" for r in named)  # the named fixer ran
    assert not any(r["fixer"] == "title_junk" for r in other)  # excluded when a different fixer is named


@loopback_only
def test_mcp_scan_resume_skips_processed(tmp_path: Path) -> None:
    # Resume flows through MCP: a second scan over the same CSV skips the processed file, staging nothing.
    root = _junk_lib(tmp_path)
    review = str(tmp_path / "r.csv")

    async def go() -> tuple[dict[str, object], dict[str, object]]:
        async with Client(mcp) as c:
            first = await c.call_tool("scan", {"root": root, "review": review, "online": False})
            again = await c.call_tool("scan", {"root": root, "review": review, "online": False, "resume": True})
            return dict(first.data), dict(again.data)

    first, again = _run(go())
    assert first["staged"] >= 1
    assert again["resumed"] is True
    assert again["new"] == 0  # the file was already processed -> nothing new this run
    assert again["staged"] == 1  # 'staged' is the RUNNING total (rows in the CSV), not the per-run delta


@loopback_only
def test_mcp_disambiguate_accepts_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The timeout->researcher wiring is proven in test_make_researcher_cli_wires_timeout; here it is a no-op.
    monkeypatch.setenv("TAGISTRY_DIR", str(tmp_path / "cfg"))  # keep any cache out of the real config dir
    import csv

    from tagistry import pipeline

    review = str(tmp_path / "r.csv")
    with open(review, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(pipeline.REVIEW_HEADER)
        w.writerow(["skip", "resolve_artist", "REVIEW", "a.mp3", "artist", "VA", "Real Artist", "resolve"])

    async def go() -> dict[str, object]:
        async with Client(mcp) as c:
            r = await c.call_tool(
                "disambiguate", {"review": review, "researcher": "none", "online": False, "timeout": 30}
            )
            return dict(r.data)

    data = _run(go())
    assert data["touched"] == 0  # researcher 'none' declines; the call still succeeds with a timeout arg
