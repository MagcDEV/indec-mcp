import json
import os
import sqlite3
import time
from dataclasses import replace

import httpx
import pytest
from starlette.testclient import TestClient

from indec_mcp.hosted import Settings, build_app
from indec_mcp.mirror import Mirror, QueryError, ResultCache
from indec_mcp.providers import ProviderRanges
from indec_mcp.server import create_server
from indec_mcp.usage import UsageRecorder

from .fixture_db import build

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ANTHROPIC_IPS = ["160.79.104.10", "160.79.106.77", "153.61.210.5"]  # 160.79.104.0/21, 153.61.208.0/20
OPENAI_DOC = {"creationTime": "2026-11-06", "prefixes": [{"ipv4Prefix": "100.31.168.162/32"},
                                                         {"ipv4Prefix": "104.192.219.204/30"}]}


# -- provider classification --------------------------------------------------------------------


def test_classifies_published_ranges_only():
    pr = ProviderRanges(openai_doc=OPENAI_DOC)
    assert {pr.classify(ip) for ip in ANTHROPIC_IPS} == {"anthropic"}
    assert pr.classify("104.192.219.206") == "openai"
    assert pr.classify("160.79.112.1") is None  # just outside 160.79.104.0/21
    assert pr.classify("203.0.113.9") is None
    assert pr.classify("not-an-ip") is None and pr.classify("2001:db8::1") is None


def test_bundled_openai_snapshot_loads_offline():
    assert ProviderRanges().counts()["openai"] > 100


def test_refresh_replaces_list_and_caches_it_but_keeps_old_one_on_failure(tmp_path):
    pr = ProviderRanges(cache_dir=tmp_path, openai_doc=OPENAI_DOC)
    new = {"creationTime": "x", "prefixes": [{"ipv4Prefix": "198.51.100.0/24"}]}
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=new)))
    assert pr.refresh_openai(ok) == 1 and pr.classify("198.51.100.7") == "openai"
    assert json.loads((tmp_path / "openai-chatgpt-connectors.json").read_text()) == new
    assert ProviderRanges(cache_dir=tmp_path).classify("198.51.100.7") == "openai"  # restart uses the cache
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert pr.refresh_openai(bad) == 1 and pr.classify("198.51.100.7") == "openai"


# -- rate limits by profile -----------------------------------------------------------------------


@pytest.fixture
def hosted(tmp_path):
    db = build(tmp_path / "indec.duckdb")
    usage = UsageRecorder(tmp_path / "usage.sqlite", start_thread=False)
    settings = replace(Settings(db_path=db), per_ip_burst=2, per_ip_per_minute=2, provider_burst=6,
                       provider_per_minute=6, stats_token="t")
    app = build_app(Mirror(db), settings, usage=usage, providers=ProviderRanges(openai_doc=OPENAI_DOC))
    with TestClient(app) as c:
        yield c, usage


def post(c, ip, ua=None):
    h = {**H, "x-real-ip": ip, **({"user-agent": ua} if ua else {})}
    return c.post("/mcp", headers=h, json=LIST).status_code


def test_claude_ai_users_share_one_larger_provider_bucket(hosted):
    c, _ = hosted
    codes = [post(c, ANTHROPIC_IPS[i % 3]) for i in range(7)]
    assert codes == [200] * 6 + [429]  # 6 = provider burst, across rotating IPs
    assert post(c, "104.192.219.205") == 200  # OpenAI has its own bucket
    assert [post(c, "203.0.113.5") for _ in range(3)] == [200, 200, 429]  # direct users unchanged


def test_claiming_to_be_claude_in_the_user_agent_gets_no_bigger_bucket(hosted):
    c, _ = hosted
    assert [post(c, "203.0.113.6", ua="Anthropic/ClaudeAI") for _ in range(3)] == [200, 200, 429]


def test_users_count_only_direct_clients_and_providers_are_reported_apart(hosted):
    c, usage = hosted
    for ip in ANTHROPIC_IPS:
        post(c, ip, ua="Anthropic/ClaudeAI")
    post(c, "203.0.113.7")
    r = c.get("/stats", headers={"authorization": "Bearer t"}).json()
    assert r["users"]["today"] == 1
    assert r["via_providers"] == [{"via": "anthropic", "requests": 3, "tool_calls": 0, "active_days": 1,
                                   "rate_limited": 0}]
    assert r["daily"][-1]["via_provider"] == 3
    # The UA fallback keeps the product token ("Anthropic"); real Claude.ai sends clientInfo.name.
    claude = next(x for x in r["clients"] if x["client"] == "Anthropic")
    assert claude == {"client": "Anthropic", "via": "anthropic", "users": 0, "requests": 3}


def test_v1_usage_tables_are_migrated_and_backfilled(tmp_path):
    path = tmp_path / "usage.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (ts INTEGER, day TEXT, user TEXT, client TEXT, method TEXT, tool TEXT, "
                "detail TEXT, status INTEGER, is_error INTEGER, ms INTEGER)")
    con.execute("INSERT INTO events VALUES (1, '2026-11-07', 'a', 'Anthropic/ClaudeAI', 'tools/list', NULL, NULL, 200, 0, 1)")
    con.execute("INSERT INTO events VALUES (1, '2026-11-07', 'b', 'claude-code', 'tools/list', NULL, NULL, 200, 0, 1)")
    con.commit()
    UsageRecorder(path, start_thread=False)
    assert dict(con.execute("SELECT user, via FROM events").fetchall()) == {"a": "anthropic", "b": None}


# -- result cache ---------------------------------------------------------------------------------


@pytest.fixture
def mirror(tmp_path):
    m = Mirror(build(tmp_path / "indec.duckdb"))
    yield m
    m.close()


def test_repeated_calls_hit_the_cache_until_the_mirror_file_changes(mirror, tmp_path):
    calls = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    assert mirror.cached("t", {"a": 1}, compute) == {"n": 1}
    assert mirror.cached("t", {"a": 1}, compute) == {"n": 1}
    assert mirror.cached("t", {"a": 2}, compute) == {"n": 2}  # different args
    assert mirror.cache.stats()["hits"] == 1
    new = build(tmp_path / "new.duckdb")
    os.replace(new, mirror.db_path)  # what the daily sync does
    assert mirror.cached("t", {"a": 1}, compute) == {"n": 3}
    assert mirror.cache.stats()["entries"] == 1


def test_errors_are_not_cached(mirror):
    n = []

    def boom():
        n.append(1)
        raise QueryError("x")

    for _ in range(2):
        with pytest.raises(QueryError):
            mirror.cached("t", {}, boom)
    assert len(n) == 2


def test_cache_is_bounded_by_size():
    c = ResultCache(max_bytes=100, max_entry_bytes=60)
    for i in range(5):
        c.get_or_compute("v", str(i), lambda: "x" * 40)
    c.get_or_compute("v", "big", lambda: "y" * 80)  # over the per-entry cap: returned, not stored
    s = c.stats()
    assert s["entries"] == 2 and s["mb"] * 1e6 <= 100


def test_tools_are_cached_end_to_end_and_sql_whitespace_is_normalized(mirror):
    import asyncio

    server = create_server(mirror)
    run = lambda name, args: asyncio.run(server.call_tool(name, args)).structured_content  # noqa: E731
    first = run("inflation_series", {"category": "Núcleo"})
    assert run("inflation_series", {"category": "Núcleo"}) == first
    run("query_sql", {"sql": "SELECT 1"})
    run("query_sql", {"sql": "  SELECT   1 ; "})
    assert mirror.cache.stats() == {**mirror.cache.stats(), "hits": 2, "misses": 2}
