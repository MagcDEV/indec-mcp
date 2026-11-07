import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from indec_mcp.mirror import Mirror
from indec_mcp.server import create_server

from .fixture_db import build


@pytest.fixture
def server(tmp_path):
    return create_server(Mirror(build(tmp_path / "indec.duckdb")))


def run(coro):
    return asyncio.run(coro)


def test_exposes_read_only_tools(server):
    tools = {t.name: t for t in run(server.list_tools())}
    assert set(tools) == {"latest_indicators", "inflation_series", "compare_series", "list_datasets",
                          "describe_dataset", "query_sql"}
    assert all(t.annotations.read_only_hint and not t.annotations.open_world_hint for t in tools.values())
    assert set(tools["inflation_series"].input_schema["properties"]) == {"category", "region", "measures", "start", "end"}
    assert "500 filas" in tools["query_sql"].description
    assert "Fuente: INDEC" in server.instructions


def test_tool_call_returns_structured_content(server):
    res = run(server.call_tool("inflation_series", {"category": "Núcleo", "start": "2026-08"}))
    assert not res.is_error
    assert [r["periodo"] for r in res.structured_content["rows"]] == ["2026-08", "2026-09"]


def test_data_errors_become_tool_errors_the_model_can_read(server):
    with pytest.raises(ToolError, match="Solo se permiten consultas SELECT"):
        run(server.call_tool("query_sql", {"sql": "DROP TABLE ipc_nacional"}))
