from datetime import datetime, timezone

import duckdb
import pytest

from indec_mcp.client import AuthError, IndecClient
from indec_mcp.sync import sync

from .conftest import FakeIndec, export_rows, make_spec

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
SPEC = make_spec({"/expo-bienes": ["Anio", "Mes", "Periodo", "pais_cod"], "/tiny": ["periodo"]})


def tiny_rows():
    # `area_cod` is always null, like pobreza_eph.area_geografica_cod in the real API.
    return [{"periodo": "20261S", "tasa": 31.6, "area_cod": None},
            {"periodo": "20252S", "tasa": 28.2, "area_cod": None}]


def client(fake, no_sleep, token="tok"):
    return IndecClient(token, base_url="https://api.test/v1", transport=fake.transport(), sleep=no_sleep)


def run(tmp_path, fake, no_sleep, **kw):
    db = tmp_path / "indec.duckdb"
    with client(fake, no_sleep) as c:
        return db, sync(db, c, SPEC, now=NOW, **kw)


def test_paginates_past_the_1000_row_cap_in_primary_key_order(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(2500), "/tiny": tiny_rows()})
    db, report = run(tmp_path, fake, no_sleep)

    assert report.ok
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM expo_bienes").fetchone()[0] == 2500
    expo_calls = [r for r in fake.calls if r.url.path == "/v1/expo-bienes"]
    assert [r.url.params["offset"] for r in expo_calls] == ["0", "1000", "2000"]
    assert {r.url.params["order"] for r in expo_calls} == {"Anio,Mes,Periodo,pais_cod"}
    # The expensive exact count is requested on the first page only.
    assert [r.headers.get("prefer") for r in expo_calls] == ["count=exact", None, None]


def test_normalizes_columns_and_writes_metadata(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(30), "/tiny": tiny_rows()})
    db, _ = run(tmp_path, fake, no_sleep)

    con = duckdb.connect(str(db), read_only=True)
    cols = dict(con.execute(
        "SELECT column_name, data_type FROM _columns WHERE table_name = 'expo_bienes'"
    ).fetchall())
    assert set(cols) == {"anio", "mes", "periodo", "pais_cod", "valor_fob", "valor_fob_null", "observaciones"}
    assert cols["valor_fob_null"] == "VARCHAR" and cols["observaciones"] == "VARCHAR"
    assert con.execute(
        "SELECT data_type FROM _columns WHERE table_name='tiny' AND column_name='area_cod'"
    ).fetchone()[0] == "VARCHAR"
    meta = con.execute(
        "SELECT row_count, api_total, latest_periodo, status, primary_key, warning "
        "FROM _datasets WHERE table_name = 'expo_bienes'"
    ).fetchone()
    assert meta == (30, 30, "200206", "ok", ["anio", "mes", "periodo", "pais_cod"], None)
    assert con.execute(
        "SELECT latest_periodo FROM _datasets WHERE table_name='tiny'"
    ).fetchone()[0] == "20261S"
    doc = con.execute(
        "SELECT description FROM _columns WHERE table_name='expo_bienes' AND column_name='anio'"
    ).fetchone()[0]
    assert doc == "Año de referencia"


def test_retries_transient_errors(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(10), "/tiny": tiny_rows()})
    fake.fail["/tiny"] = 2
    _, report = run(tmp_path, fake, no_sleep)
    assert report.ok


def test_failed_dataset_keeps_previous_table_marked_stale(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(10), "/tiny": tiny_rows()})
    run(tmp_path, fake, no_sleep)

    fake.tables["/expo-bienes"] = export_rows(20)
    fake.broken.add("/tiny")
    db, report = run(tmp_path, fake, no_sleep)

    assert not report.ok
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM expo_bienes").fetchone()[0] == 20  # refreshed
    assert con.execute("SELECT count(*) FROM tiny").fetchone()[0] == 2  # carried over
    status, warning = con.execute("SELECT status, warning FROM _datasets WHERE table_name='tiny'").fetchone()
    assert status == "stale" and "last sync failed" in warning


def test_only_refreshes_selected_and_carries_the_rest(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(10), "/tiny": tiny_rows()})
    run(tmp_path, fake, no_sleep)
    fake.calls.clear()

    db, report = run(tmp_path, fake, no_sleep, only={"tiny"})
    assert report.ok
    assert {r.url.path for r in fake.calls} == {"/v1/tiny"}
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT status FROM _datasets WHERE table_name='expo_bienes'").fetchone()[0] == "ok"
    assert con.execute("SELECT count(*) FROM expo_bienes").fetchone()[0] == 10


def test_auth_error_aborts_and_leaves_existing_mirror_untouched(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(10), "/tiny": tiny_rows()})
    db, _ = run(tmp_path, fake, no_sleep)
    before = db.read_bytes()

    fake.token = "rotated"
    with client(fake, no_sleep) as c, pytest.raises(AuthError):
        sync(db, c, SPEC, now=NOW)
    assert db.read_bytes() == before
    assert not (tmp_path / "indec.duckdb.tmp").exists()


def test_duplicate_primary_keys_are_flagged(tmp_path, no_sleep):
    fake = FakeIndec({"/expo-bienes": export_rows(5) + export_rows(1), "/tiny": tiny_rows()})
    db, report = run(tmp_path, fake, no_sleep)
    (o,) = [o for o in report.outcomes if o.table == "expo_bienes"]
    assert o.status == "ok" and "1 duplicate primary keys" in o.warning
