"""Spawn the real `indec-mcp` executable and talk MCP to it over stdio, like Claude Code does."""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .fixture_db import build

EXE = shutil.which("indec-mcp", path=str(Path(sys.executable).parent))


@pytest.mark.skipif(EXE is None, reason="indec-mcp not installed (pip install -e .)")
def test_stdio_server_end_to_end(tmp_path):
    db = build(tmp_path / "indec.duckdb")
    params = StdioServerParameters(command=EXE, env={"INDEC_DB": str(db)})

    async def go():
        async with Client(params, read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            snap = await client.call_tool("latest_indicators", {})
            bad = await client.call_tool("query_sql", {"sql": "DELETE FROM pobreza_eph"})
            return names, snap, bad

    names, snap, bad = asyncio.run(go())
    assert "inflation_series" in names and "query_sql" in names
    assert not snap.is_error
    assert snap.structured_content["inflacion_ipc_nacional"]["periodo"] == "2026-09"
    # Over the wire, a ToolError arrives as an error result the model can read, not a crash.
    assert bad.is_error
    assert "Solo se permiten consultas SELECT" in bad.content[0].text
