import pytest

from indec_mcp import topics as T
from indec_mcp.mirror import Mirror, QueryError

from .fixture_db import build


@pytest.fixture
def m(tmp_path):
    mirror = Mirror(build(tmp_path / "indec.duckdb"))
    yield mirror
    mirror.close()


# -- trade codes --------------------------------------------------------------------------------


def test_trade_codes_search_is_accent_and_case_insensitive(m):
    assert [c["codigo"] for c in T.trade_codes(m, "brasil")] == ["BR"]
    assert T.trade_codes(m, "CN")[0]["descripcion"] == "China"


@pytest.mark.parametrize(
    "country, code_name",
    [("China", "China"),  # must not also match "Hong Kong ... (China)"
     ("hong kong", "Hong Kong Región Administrativa Especial de (China)"),
     ("EE.UU.", "Estados Unidos"), ("usa", "Estados Unidos")],
)
def test_country_resolution(m, country, code_name):
    flow = "importaciones" if code_name == "Estados Unidos" else "exportaciones"
    assert T.foreign_trade(m, flow, country=country)["filtros"]["pais"] == code_name


def test_acronym_in_parentheses_resolves_the_rubro(m):
    r = T.foreign_trade(m, category="MOA")
    assert r["filtros"]["categoria"].startswith("MANUFACTURAS DE ORIGEN AGROPECUARIO (MOA)")


def test_unknown_names_point_to_trade_codes(m):
    with pytest.raises(QueryError, match="trade_codes"):
        T.foreign_trade(m, country="Atlantis")


# -- foreign trade ------------------------------------------------------------------------------


def test_default_is_last_12_months_by_month_with_confidential_rows_reported(m):
    r = T.foreign_trade(m)
    assert (r["desde"], r["hasta"]) == ("2025-10", "2026-09") and len(r["rows"]) == 12
    assert r["rows"][-1]["millones_usd"] == 301.0  # 100 + 200 + 1; the confidential row adds nothing
    assert r["rows"][-1]["filas_confidenciales"] == 1
    assert r["porcentaje_filas_confidenciales"] == 25.0
    assert "EXCLUYEN" in r["nota"]


def test_group_by_country_names_and_orders_by_value(m):
    r = T.foreign_trade(m, group_by="pais", start="2025", end="2025", top=2)
    assert [x["grupo"] for x in r["rows"]] == ["China", "Brasil"]
    assert r["rows"][0]["millones_usd"] == 2400.0  # 12 months x 200


def test_imports_by_category_and_year(m):
    r = T.foreign_trade(m, "impo", category="bienes de capital", group_by="anio", start="2025-01")
    assert [x["grupo"] for x in r["rows"]] == ["2025", "2026"]
    assert r["rows"][0]["millones_usd"] == 360.0


@pytest.mark.parametrize("kwargs, match", [({"flow": "otro"}, "flow"), ({"group_by": "dia"}, "group_by"),
                                           ({"start": "2030-01"}, "No hay datos")])
def test_trade_validation(m, kwargs, match):
    with pytest.raises(QueryError, match=match):
        T.foreign_trade(m, **kwargs)


# -- population ---------------------------------------------------------------------------------


def test_country_total_and_age_groups(m):
    p = T.population(m, year=2026)
    assert p["lugar"] == "Argentina" and p["total"] == 2 * (1000 + 1004 + 1005 + 1100)
    ages = T.population(m, year=2026, by="edad")["rows"]
    assert [a["grupo"] for a in ages] == ["0 a 4", "5 a 9", "100 y más"]


def test_province_by_alias_and_age_groups_in_numeric_order(m):
    p = T.population(m, "caba", 2026)
    assert p["lugar"] == "Ciudad Autónoma de Buenos Aires" and p["total"] == 3000
    ages = [r["grupo"] for r in T.population(m, "Cordoba", 2026, by="edad")["rows"]]
    assert ages == ["0 a 4 años", "5 a 9 años", "10 a 14 años"]


def test_departments_of_a_province_and_disambiguation(m):
    deps = T.population(m, "Córdoba", 2026, by="departamento")["rows"]
    assert deps[0] == {"grupo": "Capital", "poblacion": 1400}
    with pytest.raises(QueryError, match="ambiguo.*San Martín, Córdoba.*San Martín, Mendoza"):
        T.population(m, "San Martín", 2026)
    assert T.population(m, "San Martín, Mendoza", 2026)["total"] == 140


@pytest.mark.parametrize(
    "kwargs, match",
    [({"year": 2050}, "fuera de rango"), ({"place": "Capital", "by": "edad"}, "no por departamento"),
     ({"by": "departamento"}, "indicá una provincia"), ({"place": "Narnia"}, "No encontré")],
)
def test_population_validation(m, kwargs, match):
    with pytest.raises(QueryError, match=match):
        T.population(m, **kwargs)
