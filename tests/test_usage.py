import json
import sqlite3
import time
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from indec_mcp.hosted import Settings, build_app
from indec_mcp.mirror import Mirror
from indec_mcp.usage import UsageRecorder, tool_detail

from .fixture_db import build

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def call(tool, args, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": tool, "arguments": args}}


INIT = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "claude-code", "version": "2.1.319"}}}


@pytest.fixture
def env(tmp_path):
    db = build(tmp_path / "indec.duckdb")
    usage = UsageRecorder(tmp_path / "usage.sqlite", start_thread=False)
    settings = replace(Settings(db_path=db), stats_token="s3cret", per_ip_burst=5, per_ip_per_minute=5)
    with TestClient(build_app(Mirror(db), settings, usage=usage)) as c:
        yield c, usage


def rows(usage):
    usage.flush()
    con = sqlite3.connect(usage.db_path)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY rowid")]


def test_records_client_tool_topic_and_never_the_ip(env):
    c, usage = env
    ip = {"x-real-ip": "203.0.113.9"}
    c.post("/mcp", headers={**H, **ip, "user-agent": "claude-code/2.1.319 (sdk-cli)"}, json=INIT)
    c.post("/mcp", headers={**H, **ip}, json=call("inflation_series", {"category": "Núcleo", "region": "GBA"}))
    c.post("/mcp", headers={**H, **ip}, json=call("query_sql", {"sql": "SELECT secret_text FROM pobreza_eph p "
                                                                       "JOIN ipc_nacional i ON true WHERE 'my private note'"}))
    ev = rows(usage)
    assert [e["method"] for e in ev] == ["initialize", "tools/call", "tools/call"]
    assert {e["client"] for e in ev} == {"claude-code"}  # remembered for later calls without clientInfo
    assert ev[1]["tool"] == "inflation_series" and ev[1]["detail"] == "Núcleo@GBA" and ev[1]["is_error"] == 0
    assert ev[2]["detail"] == "tables:ipc_nacional,pobreza_eph"  # never the SQL text
    dump = json.dumps(ev)
    assert "203.0.113.9" not in dump and "private note" not in dump
    assert len({e["user"] for e in ev}) == 1 and len(ev[0]["user"]) == 16


def test_tool_errors_and_rate_limits_are_counted(env):
    c, usage = env
    ip = {"x-real-ip": "198.51.100.4"}
    c.post("/mcp", headers={**H, **ip}, json=call("query_sql", {"sql": "DROP TABLE x"}))
    for _ in range(6):
        c.post("/mcp", headers={**H, **ip}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ev = rows(usage)
    assert ev[0]["is_error"] == 1 and ev[0]["status"] == 200
    assert [e["status"] for e in ev].count(429) >= 1


def test_users_are_distinct_per_ip_and_stable(env):
    c, usage = env
    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.1"):
        c.post("/mcp", headers={**H, "x-real-ip": ip}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ev = rows(usage)
    assert ev[0]["user"] == ev[2]["user"] != ev[1]["user"]


def test_client_name_from_new_protocol_meta_and_user_agent_fallback(env):
    c, usage = env
    discover = {"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                "params": {"_meta": {"io.modelcontextprotocol/clientInfo": {"name": "cursor", "version": "3"}}}}
    c.post("/mcp", headers={**H, "x-real-ip": "203.0.113.20"}, json=discover)
    c.post("/mcp", headers={**H, "x-real-ip": "203.0.113.21", "user-agent": "openai-mcp/1.0"},
           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [e["client"] for e in rows(usage)] == ["cursor", "openai-mcp"]


def test_oversized_bodies_still_get_413_through_the_usage_middleware(env):
    c, _ = env
    assert c.post("/mcp", headers=H, content=b"x" * (70 * 1024)).status_code == 413


def test_stats_endpoint_requires_token_and_reports(env):
    c, _ = env
    for ip in ("203.0.113.1", "203.0.113.2"):
        c.post("/mcp", headers={**H, "x-real-ip": ip}, json=call("population", {"place": "Córdoba"}))
    assert c.get("/stats").status_code == 401
    assert c.get("/stats", headers={"authorization": "Bearer nope"}).status_code == 401
    r = c.get("/stats", headers={"authorization": "Bearer s3cret"}).json()
    assert r["users"]["today"] == 2 and r["daily"][-1]["new_users"] == 2
    assert r["tools"][0]["tool"] == "population" and r["tools"][0]["users"] == 2
    assert r["top_topics"][0]["detail"] == "Córdoba"


def test_stats_is_hidden_without_a_configured_token(tmp_path):
    db = build(tmp_path / "indec.duckdb")
    usage = UsageRecorder(tmp_path / "usage.sqlite", start_thread=False)
    with TestClient(build_app(Mirror(db), Settings(db_path=db), usage=usage)) as c:
        assert c.get("/stats", headers={"authorization": "Bearer "}).status_code == 404


def test_salt_persists_and_retention_prunes_old_events(tmp_path):
    a = UsageRecorder(tmp_path / "u.sqlite", start_thread=False)
    b = UsageRecorder(tmp_path / "u.sqlite", start_thread=False)
    assert a.user_id("1.2.3.4") == b.user_id("1.2.3.4") != a.user_id("1.2.3.5")
    a.record(user="u", client=None, method="tools/list", tool=None, detail=None, status=200, is_error=False, ms=1)
    a.flush()
    con = sqlite3.connect(a.db_path)
    con.execute("UPDATE events SET day = '2000-01-01'")
    con.commit()
    a.prune()
    assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize(
    "tool, args, expected",
    [("compare_series", {"series": ["salarios", "ipc"]}, "salarios,ipc"),
     ("foreign_trade", {"country": "China", "group_by": "anio"}, "exportaciones|China|anio"),
     ("population", {}, "Argentina"),
     ("describe_dataset", {"table": "ipc_nacional"}, "ipc_nacional"),
     ("query_sql", {"sql": "select 1"}, "tables:?"),
     ("latest_indicators", {}, None)],
)
def test_tool_detail(tool, args, expected):
    assert tool_detail(tool, args) == expected
