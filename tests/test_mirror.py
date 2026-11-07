import os
import shutil

import pytest

from indec_mcp.mirror import Limits, Mirror, QueryError, format_period, parse_period

from .fixture_db import build


@pytest.fixture
def mirror(tmp_path):
    m = Mirror(build(tmp_path / "indec.duckdb"), Limits(max_rows=50, timeout_s=0.5))
    yield m
    m.close()


# -- query_sql sandbox ------------------------------------------------------------------------


def test_select_works_and_reports_source(mirror):
    res = mirror.query("SELECT count(*) AS n FROM pobreza_eph;")
    assert res["columns"] == ["n"] and res["rows"] == [[3]]
    assert "INDEC" in res["source"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE ipc_nacional",
        "INSERT INTO pobreza_eph SELECT * FROM pobreza_eph",
        "SELECT 1; SELECT 2",
        "PRAGMA version",
        "SET enable_external_access = true",
        "ATTACH '/tmp/other.duckdb'",
        "COPY (SELECT 1) TO '/tmp/out.csv'",
    ],
)
def test_non_select_and_multi_statements_are_rejected(mirror, sql):
    with pytest.raises(QueryError):
        mirror.query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM '/etc/hostname'",
        "SELECT * FROM read_json('https://example.com/x.json')",
    ],
)
def test_file_and_network_access_is_blocked_inside_select(mirror, sql):
    with pytest.raises(QueryError, match="(?i)permission|access|does not exist"):
        mirror.query(sql)


def test_rows_are_capped_and_flagged(mirror):
    res = mirror.query("SELECT * FROM range(1000)")
    assert res["row_count"] == 50 and res["truncated"] is True
    assert "truncado" in res["note"]


def test_slow_queries_are_interrupted(mirror):
    with pytest.raises(QueryError, match="límite"):
        mirror.query("SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000) b")
    # The connection is still usable afterwards.
    assert mirror.query("SELECT 42")["rows"] == [[42]]


def test_reopens_after_the_file_is_atomically_replaced(mirror, tmp_path):
    assert mirror.query("SELECT count(*) FROM pobreza_eph")["rows"] == [[3]]
    new = build(tmp_path / "new.duckdb")
    import duckdb

    con = duckdb.connect(str(new))
    con.execute("DELETE FROM pobreza_eph WHERE periodo = '20252S'")
    con.close()
    os.replace(new, mirror.db_path)
    assert mirror.query("SELECT count(*) FROM pobreza_eph")["rows"] == [[2]]


def test_missing_mirror_gives_a_clear_error(tmp_path):
    with pytest.raises(QueryError, match="no están disponibles"):
        Mirror(tmp_path / "nope.duckdb").datasets()


# -- catalog ----------------------------------------------------------------------------------


def test_describe_lists_exact_dimension_values_and_hides_null_siblings(mirror):
    d = mirror.describe("pobreza_eph")
    cols = {c["column_name"]: c for c in d["columns"]}
    assert "tasa_pobreza_personas_null" not in cols
    assert cols["area_geografica_nom"]["values"] == ["T_31_A"]
    assert "values" not in cols["periodo"]
    assert d["latest_period"] == "2026 S1"
    assert "tasa_pobreza_personas_null" not in d["sample_rows"]["columns"]


def test_unknown_dataset_lists_alternatives(mirror):
    with pytest.raises(QueryError, match="ipc_nacional"):
        mirror.describe("inflacion")


# -- inflation_series -------------------------------------------------------------------------


def test_ipc_series_pivots_measures_and_defaults_to_last_13_months(mirror):
    s = mirror.ipc_series()
    assert s["category"] == "Nivel general" and s["region"] == "Nacional"
    periods = [r["periodo"] for r in s["rows"]]
    assert periods[0] == "2025-09" and periods[-1] == "2026-09" and len(periods) == 13
    last = s["rows"][-1]
    assert set(last) == {"periodo", "v_m", "v_ia", "observaciones"}
    assert last["observaciones"] == "Dato provisorio"


def test_ipc_series_respects_range_and_measures(mirror):
    s = mirror.ipc_series("núcleo", measures=["Indice"], start="2025-01", end="2025/03")
    assert [r["periodo"] for r in s["rows"]] == ["2025-01", "2025-02", "2025-03"]
    assert set(s["rows"][0]) == {"periodo", "Indice", "observaciones"}


def test_ipc_category_resolution_is_region_aware(mirror):
    # Nacional has no Grupo detail, so 'alimentos' must resolve to the División.
    assert mirror.ipc_series("alimentos")["level"] == "División"
    assert mirror.ipc_series("alimentos", region="gba")["level"] == "Grupo"


def test_ipc_ambiguous_and_unknown_categories_explain_options(mirror):
    with pytest.raises(QueryError, match="ambiguo.*Opciones"):
        mirror.ipc_series("bienes y", region="GBA")
    with pytest.raises(QueryError, match="Disponibles: .*Nivel general"):
        mirror.ipc_series("zzz")


@pytest.mark.parametrize(
    "kwargs, match",
    [({"region": "Mendoza"}, "Región inválida"), ({"measures": ["yoy"]}, "Medidas inválidas"),
     ({"start": "sept 2025"}, "Período inválido")],
)
def test_ipc_validates_inputs(mirror, kwargs, match):
    with pytest.raises(QueryError, match=match):
        mirror.ipc_series(**kwargs)


# -- snapshot ---------------------------------------------------------------------------------


def test_snapshot_has_headline_figures_with_periods_and_flags(mirror):
    s = mirror.snapshot()
    assert s["inflacion_ipc_nacional"]["periodo"] == "2026-09"
    assert s["actividad_emae"] == {"periodo": "2026-08", "interanual_pct": -1.7, "mensual_desestacionalizada_pct": 0.1}
    assert s["salarios_nivel_general"]["interanual_pct"] == 35.61
    assert s["canastas_gba"]["cbt_hogar_tipo_2_ars"] == 1632504.69
    assert s["pobreza_31_aglomerados"]["observaciones"] == "Dato provisorio"
    assert s["mercado_de_trabajo_31_aglomerados"]["desocupacion_pct"] == 7.9
    assert s["synced_at"].startswith("2026-11-07")


# -- helpers ----------------------------------------------------------------------------------


def test_period_formatting_round_trips():
    assert format_period("202609") == "2026-09"
    assert format_period("20261S") == "2026 S1"
    assert format_period("20262T") == "2026 T2"
    assert parse_period("2026-9") == "202609"
    assert parse_period(None) is None


# -- concurrency ------------------------------------------------------------------------------

SLOW = "SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000) b"


def _in_background(fn):
    import threading

    box = {}
    t = threading.Thread(target=lambda: box.setdefault("err", _catch(fn)))
    t.start()
    return t, box


def _catch(fn):
    try:
        fn()
    except QueryError as e:
        return e


def test_fast_queries_are_not_blocked_by_a_slow_one(tmp_path):
    import time

    m = Mirror(build(tmp_path / "indec.duckdb"), Limits(timeout_s=1.5, max_concurrent=2))
    t, box = _in_background(lambda: m.query(SLOW))
    time.sleep(0.2)
    t0 = time.monotonic()
    assert m.query("SELECT 42")["rows"] == [[42]]
    assert time.monotonic() - t0 < 0.5  # did not wait for the slow query's 1.5s timeout
    t.join()
    assert "límite" in str(box["err"])


def test_when_all_slots_are_busy_callers_get_a_clear_error(tmp_path):
    import time

    m = Mirror(build(tmp_path / "indec.duckdb"), Limits(timeout_s=1.0, max_concurrent=1, queue_timeout_s=0.1))
    t, _ = _in_background(lambda: m.query(SLOW))
    time.sleep(0.2)
    with pytest.raises(QueryError, match="ocupado"):
        m.query("SELECT 1")
    t.join()
    assert m.query("SELECT 1")["rows"] == [[1]]  # free again


# -- compare ----------------------------------------------------------------------------------


def test_compare_aligns_on_common_months_and_uses_published_changes(mirror):
    r = mirror.compare(["salarios", "ipc"], "v_ia")
    # Wages end in 2026-08, IPC in 2026-09: only common months, last 13 by default.
    periods = [row["periodo"] for row in r["rows"]]
    assert periods[-1] == "2026-08" and len(periods) == 13
    assert r["rows"][-1]["Salarios: Nivel general"] == 35.61
    assert {s["ultimo_periodo"] for s in r["series"]} == {"2026-08", "2026-09"}


def test_compare_summary_is_computed_from_levels_and_labelled(mirror):
    r = mirror.compare(["salarios", "ipc"], "base100", start="2025-01", end="2025-04")
    assert r["rows"][0]["Salarios: Nivel general"] == 100.0
    # Wages 2000 -> 2120 (+6.0%); IPC 1000+12 -> 1000+15 in the fixture.
    ch = r["summary"]["variacion_entre_niveles_pct"]
    assert ch["Salarios: Nivel general"] == 6.0
    assert ch["IPC Nivel general (Nacional)"] == round((1015 / 1012 - 1) * 100, 1)
    assert "CALCULADO" in r["summary"]["nota"]
    assert r["summary"]["relativo_pct"] == round((1.06 / (1 + ch["IPC Nivel general (Nacional)"] / 100) - 1) * 100, 1)


def test_compare_resolves_series_ids(mirror):
    r = mirror.compare(["salarios:registrado", "cbt", "ipc:Núcleo@GBA", "emae"], "v_m", start="2026-01")
    labels = [s["label"] for s in r["series"]]
    assert labels == ["Salarios: Total registrado", "CBT por adulto equivalente (GBA, $)",
                      "IPC Núcleo (GBA)", "EMAE nivel general"]
    assert r["rows"][-1]["EMAE nivel general"] == 0.07  # rounded
    assert any("desestacionalizada" in n for n in r["notes"])


def test_compare_handles_series_without_index_level(mirror):
    r = mirror.compare(["salarios:público nacional", "ipc"], "base100", start="2026-01")
    assert r["summary"]["variacion_entre_niveles_pct"]["Salarios: Subsector público nacional"] is None
    assert "Sin índice publicado" in r["summary"]["sin_nivel"]
    assert "relativo_pct" not in r["summary"]


@pytest.mark.parametrize(
    "args, match",
    [((["ipc"],), "entre 2 y 4"), ((["ipc", "pbi"],), "Serie desconocida"),
     ((["ipc", "salarios:jubilados"],), "no encontrado"), ((["ipc", "cbt"], "yoy"), "measure"),
     ((["ipc@Mendoza", "cbt"],), "Región inválida"), ((["ipc", "cbt"], "v_ia", "2020-01", "2020-06"), "en común")],
)
def test_compare_validates(mirror, args, match):
    with pytest.raises(QueryError, match=match):
        mirror.compare(*args)
