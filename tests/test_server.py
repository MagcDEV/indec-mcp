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
    assert set(tools) == {"latest_indicators", "inflation_series", "compare_series", "foreign_trade",
                          "trade_codes", "population", "wholesale_prices", "construction_costs",
                          "industrial_production", "wages", "economic_activity", "basic_baskets", "poverty",
                          "labor_market", "list_datasets", "describe_dataset", "query_sql"}
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


def test_prompts_and_resources_are_exposed(server):
    prompts = {p.name for p in run(server.list_prompts())}
    assert prompts == {"informe_inflacion", "panorama_economico", "evolucion_pobreza",
                       "comercio_exterior_pais", "salario_real"}
    msg = run(server.get_prompt("comercio_exterior_pais", {"pais": "Chile"})).messages[0]
    assert "Chile" in msg.content.text and "confidenciales" in msg.content.text
    assert {str(r.uri) for r in run(server.list_resources())} == {"indec://guia", "indec://datasets"}
    guide = run(server.read_resource("indec://guia"))[0].content
    assert "secreto estadístico" in guide
    schema = run(server.read_resource("indec://dataset/pobreza_eph"))[0].content
    assert "area_geografica_nom" in schema


def test_every_tool_has_a_title_and_read_only_annotation_for_directory_review(server):
    # Anthropic's Connectors Directory rejects tools without a title and readOnlyHint/destructiveHint.
    for t in run(server.list_tools()):
        assert t.title, t.name
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False, t.name
