import asyncio

import pytest

from indec_mcp import indices as I
from indec_mcp.mirror import Mirror, QueryError
from indec_mcp.server import create_server

from .fixture_db import build


@pytest.fixture
def m(tmp_path):
    mirror = Mirror(build(tmp_path / "indec.duckdb"))
    yield mirror
    mirror.close()


def test_wholesale_defaults_to_general_level_last_13_months(m):
    r = I.series(m, "IPIM")
    assert r["categoria"] == "Nivel general" and r["codigo"] == "NG"
    assert [x["periodo"] for x in r["rows"]][0] == "2025-09" and len(r["rows"]) == 13
    last = r["rows"][-1]
    assert (last["v_m"], last["v_ia"], last["observaciones"]) == (2.0, 30.0, "* Dato provisorio")


def test_each_wholesale_index_is_its_own_series(m):
    assert I.series(m, "IPP", start="2026-09")["rows"][0]["v_m"] == 2.2
    assert I.series(m, "IPIB", start="2026-09")["rows"][0]["v_m"] == 2.1


def test_wholesale_ambiguity_lists_codes_and_code_resolves(m):
    with pytest.raises(QueryError, match=r"ambiguo.*Origen, código I.*Sector, código 3"):
        I.series(m, "IPIM", "Productos importados")
    assert I.series(m, "IPIM", "I")["nivel"] == "Origen"
    assert I.series(m, "IPIB", "cereales y oleaginosas")["codigo"] == "0111"  # accent/case-insensitive


def test_construction_chapters_items_and_measures(m):
    r = I.series(m, "ICC", "mano de obra", ["v_m", "v_ia", "v_acum"], start="2026-09")
    assert r["nivel"] == "Capitulo" and r["rows"] == [{"periodo": "2026-09", "v_m": 1.6, "v_ia": 31.0, "v_acum": 25.0}]
    assert I.series(m, "ICC", "Pintura")["codigo"] == "IT-12"
    with pytest.raises(QueryError, match="Algunas opciones: .*Mano de obra"):
        I.series(m, "ICC", "zzz")


def test_industry_mixes_original_and_seasonally_adjusted_and_says_so(m):
    r = I.series(m, "IPI_MANUFACTURERO", start="2026-09")
    assert r["rows"] == [{"periodo": "2026-09", "v_ia": -1.78, "v_m": 0.73}]  # rounded
    assert "desestacionalizada" in r["medidas"]["v_m"] and "original" in r["medidas"]["v_ia"]


def test_branch_without_seasonal_series_explains_missing_measure(m):
    r = I.series(m, "IPI_MANUFACTURERO", "vino", start="2026-09")
    assert r["rows"][0]["v_m"] is None and any("Sin datos de v_m" in n for n in r["notas"])
    with pytest.raises(QueryError, match="desestacionalizadas solo existen"):
        I.series(m, "IPI_MANUFACTURERO", "vino", ["v_m"])


def test_mining(m):
    assert I.series(m, "IPI_MINERO", start="2026-09")["rows"][0] == {"periodo": "2026-09", "v_ia": 7.13, "v_m": -0.09}
    assert I.series(m, "IPI_MINERO", "petróleo crudo", ["v_ia"])["codigo"] == "06.100"


def test_invalid_measures(m):
    with pytest.raises(QueryError, match="Medidas inválidas"):
        I.series(m, "ICC", measures=["yoy"])


def test_compare_series_accepts_new_indices(m):
    r = m.compare(["ipim", "ipc"], "v_ia", start="2026-09")
    assert r["rows"][0]["IPIM: Nivel general"] == 30.0
    r = m.compare(["icc:Mano de obra", "salarios"], "base100", start="2026-01")
    assert r["series"][0]["label"] == "Costo construcción: Mano de obra"
    r = m.compare(["ipi", "ipi_minero"], "v_m", start="2026-09")
    assert r["rows"][0] == {"periodo": "2026-09", "IPI manufacturero: Nivel general": 0.73, "IPI minero: Nivel general": -0.09}
    assert any("desestacionalizada" in n for n in r["notes"])


def test_snapshot_includes_wholesale_construction_and_industry(m):
    s = m.snapshot()
    assert s["precios_mayoristas_ipim"]["mensual_pct"] == 2.0
    assert s["costo_construccion_icc_gba"]["interanual_pct"] == 31.0
    assert s["industria_ipi_manufacturero"]["interanual_pct"] == -1.78
    assert "nota" not in s["precios_mayoristas_ipim"]  # the IPI note must not leak onto IPIM


def test_tools_via_mcp(m):
    server = create_server(m)
    call = lambda n, a: asyncio.run(server.call_tool(n, a)).structured_content  # noqa: E731
    assert call("wholesale_prices", {"index": "ipp"})["indice"].startswith("IPP")
    assert call("construction_costs", {"category": "Pintura"})["categoria"] == "Pintura"
    assert call("industrial_production", {"sector": "minería"})["indice"].startswith("IPI minero")
