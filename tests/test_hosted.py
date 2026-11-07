from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from indec_mcp.hosted import MAX_BODY_BYTES, Settings, build_app
from indec_mcp.mirror import Mirror

from .fixture_db import build

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "latest_indicators", "arguments": {}}}


def client(db, **overrides):
    settings = replace(Settings(db_path=db), **overrides)
    return TestClient(build_app(Mirror(db), settings))


@pytest.fixture
def db(tmp_path):
    return build(tmp_path / "indec.duckdb")


def test_mcp_over_http_initialize_and_call(db):
    with client(db) as c:
        assert c.post("/mcp", headers=H, json=INIT).json()["result"]["serverInfo"]["name"] == "indec"
        res = c.post("/mcp", headers=H, json=CALL).json()["result"]
        assert not res.get("isError")
        assert res["structuredContent"]["inflacion_ipc_nacional"]["periodo"] == "2026-09"


def test_health_reports_readiness(db, tmp_path):
    with client(db) as c:
        h = c.get("/healthz").json()
        assert h["mirror_ready"] is True and h["datasets"] == 6
    with client(tmp_path / "missing.duckdb") as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json()["mirror_ready"] is False


def test_tools_explain_when_first_sync_is_pending(tmp_path):
    with client(tmp_path / "missing.duckdb") as c:
        res = c.post("/mcp", headers=H, json=CALL).json()["result"]
        assert res["isError"] and "sincronización en curso" in res["content"][0]["text"]


def test_landing_page_advertises_https_endpoint(db):
    with client(db) as c:
        j = c.get("/", headers={"x-forwarded-proto": "https", "host": "indec.up.railway.app"}).json()
        assert j["mcp_endpoint"] == "https://indec.up.railway.app/mcp"
        assert "no oficial" in j["disclaimer"].lower()


def test_per_ip_burst_is_enforced_and_other_ips_are_unaffected(db):
    with client(db, per_ip_burst=3, per_ip_per_minute=3) as c:
        a = {**H, "x-forwarded-for": "203.0.113.1"}
        codes = [c.post("/mcp", headers=a, json=INIT).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        r = c.post("/mcp", headers=a, json=INIT)
        assert int(r.headers["retry-after"]) >= 1 and r.json()["error"]["code"] == -32000
        # Forging XFF doesn't help: the rightmost (proxy-appended) entry is the one counted.
        forged = {**H, "x-forwarded-for": "9.9.9.9, 203.0.113.1"}
        assert c.post("/mcp", headers=forged, json=INIT).status_code == 429
        b = {**H, "x-forwarded-for": "203.0.113.2"}
        assert c.post("/mcp", headers=b, json=INIT).status_code == 200


def test_health_and_landing_are_not_rate_limited(db):
    with client(db, per_ip_burst=1, per_ip_per_minute=1) as c:
        assert all(c.get("/healthz").status_code == 200 for _ in range(5))


def test_oversized_bodies_are_rejected(db):
    with client(db) as c:
        assert c.post("/mcp", headers=H, content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("INDEC_DB", str(tmp_path / "x.duckdb"))
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("RATE_PER_IP_PER_MINUTE", "5")
    monkeypatch.setenv("SYNC_ENABLED", "0")
    s = Settings.from_env()
    assert (s.port, s.per_ip_per_minute, s.sync_enabled) == (9999, 5.0, False)
    assert s.db_path == tmp_path / "x.duckdb"
