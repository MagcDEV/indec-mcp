import asyncio

import pytest

from indec_mcp import indices as I
from indec_mcp import living as L
from indec_mcp.mirror import Mirror, QueryError
from indec_mcp.server import create_server

from .fixture_db import build


@pytest.fixture
def m(tmp_path):
    mirror = Mirror(build(tmp_path / "indec.duckdb"))
    yield mirror
    mirror.close()


def test_cbt_has_adult_and_household_values_and_derived_equivalences(m):
    r = L.basic_baskets(m, "CBT")
    last = r["rows"][-1]
    assert last["periodo"] == "2026-09" and last["adulto_equivalente_ars"] == 528318.67
    assert last["hogar_2_ars"] == 1632504.69 and last["v_ia"] == 38.7
    assert "hogar 2 = 3.09 adultos equivalentes" in r["notas"][1]  # derived from the data, not asserted
    assert len(r["rows"]) == 13  # default: last 13 months


def test_cba_is_the_indigence_line(m):
    r = L.basic_baskets(m, "cba", start="2026-09")
    assert r["rows"][0]["adulto_equivalente_ars"] == 239058.22 and "indigencia" in r["notas"][0]


def test_child_raising_basket_totals_and_one_age_group(m):
    r = L.basic_baskets(m, "crianza", start="2026-09")
    assert r["rows"][0]["total_1a3_ars"] == 679815.57
    r = L.basic_baskets(m, "crianza", "1 a 3 años", start="2026-09")
    assert r["rows"] == [{"periodo": "2026-09", "bienes_y_servicios_ars": 238764.09, "cuidado_ars": 441051.48,
                          "total_ars": 679815.57}]
    with pytest.raises(QueryError, match="age_group"):
        L.basic_baskets(m, "crianza", "15 años")


def test_poverty_full_series_by_default_and_year_filter(m):
    r = L.poverty(m)
    assert [x["periodo"] for x in r["rows"]] == ["2016 S2", "2025 S2", "2026 S1"]
    assert r["rows"][-1]["observaciones"] == "Dato provisorio" and "observaciones" not in r["rows"][0]
    assert [x["periodo"] for x in L.poverty(m, start="2026")["rows"]] == ["2026 S1"]
    assert [x["periodo"] for x in L.poverty(m, end="2025")["rows"]] == ["2016 S2", "2025 S2"]


def test_labor_market(m):
    r = L.labor_market(m, start="2026")
    assert r["rows"][-1]["desocupacion_pct"] == 7.9 and r["rows"][-1]["empleo_formal_pct"] == 24.7


def test_wages_engine_and_lag_note(m):
    r = I.series(m, "SALARIOS", "200", ["v_ia"], start="2026-08")
    assert r["categoria"] == "Total no registrado" and r["rows"][0]["v_ia"] == 55.36
    assert any("rezago" in n for n in r["notas"])


def test_emae_general_has_no_categories_and_sectors_resolve(m):
    with pytest.raises(QueryError, match="no tiene categorías"):
        I.series(m, "EMAE_GENERAL", "Construcción")
    r = I.series(m, "EMAE_SECTORES", "construccion", start="2026-08")
    assert r["codigo"] == "F" and r["rows"][0]["v_ia"] == -2.55


def test_compare_with_emae_sector(m):
    r = m.compare(["emae:Construcción", "icc"], "v_ia", start="2026-08", end="2026-08")
    assert r["rows"][0]["EMAE: Construcción"] == -2.55
    assert any("no hay variación mensual" in n for n in r["notes"])


def test_new_tools_via_mcp(m):
    server = create_server(m)
    call = lambda n, a: asyncio.run(server.call_tool(n, a)).structured_content  # noqa: E731
    assert call("wages", {"sector": "registrado", "start": "2026-08"})["categoria"] == "Total registrado"
    assert call("economic_activity", {"sector": "Comercio"})["codigo"] == "G"
    assert call("economic_activity", {})["categoria"] == "Nivel general"
    assert call("basic_baskets", {"basket": "CBT"})["region"] == "Gran Buenos Aires"
    assert call("poverty", {})["cobertura"].startswith("Total de 31")
    assert call("labor_market", {})["rows"][-1]["periodo"] == "2026 T2"
