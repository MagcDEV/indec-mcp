import gzip
import os
import shutil
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from indec_mcp import paths
from indec_mcp.hosted import Settings, build_app
from indec_mcp.mirror import Mirror
from indec_mcp.snapshot import DEFAULT_URL, SnapshotPublisher, SnapshotUpdater, updater_from_env, validate

from .fixture_db import build

URL = "http://testserver/snapshot/indec.duckdb.gz"


@pytest.fixture
def served(tmp_path):
    (tmp_path / "server").mkdir()
    return build(tmp_path / "server" / "indec.duckdb")


def app_client(db, **overrides):
    return TestClient(build_app(Mirror(db), replace(Settings(db_path=db), **overrides)))


def test_publisher_rebuilds_only_when_the_mirror_changes(served):
    pub = SnapshotPublisher(served)
    gz, etag = pub.current()
    assert gzip.decompress(gz.read_bytes()) == served.read_bytes()
    mtime = gz.stat().st_mtime_ns
    assert pub.current() == (gz, etag) and gz.stat().st_mtime_ns == mtime  # cached
    # indec-sync replaces the file atomically -> new etag, new gzip
    tmp = served.with_suffix(".tmp")
    shutil.copy(served, tmp)
    os.replace(tmp, served)
    assert pub.current()[1] != etag


def test_snapshot_route_serves_gzip_with_etag_and_304(served):
    with app_client(served) as c:
        r = c.get("/snapshot/indec.duckdb.gz")
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        # httpx doesn't auto-decompress: the body is the gzip file itself
        assert gzip.decompress(r.content) == served.read_bytes()
        tag = r.headers["etag"]
        r2 = c.get("/snapshot/indec.duckdb.gz", headers={"if-none-match": tag})
        assert r2.status_code == 304 and not r2.content


def test_snapshot_route_without_mirror_or_disabled(tmp_path, served):
    with app_client(tmp_path / "missing.duckdb") as c:
        assert c.get("/snapshot/indec.duckdb.gz").status_code == 503
    with app_client(served, snapshot_enabled=False) as c:
        assert c.get("/snapshot/indec.duckdb.gz").status_code == 404


def test_snapshot_route_is_rate_limited_separately(served):
    with app_client(served, snapshot_per_ip_per_hour=2) as c:
        assert [c.get("/snapshot/indec.duckdb.gz").status_code for _ in range(3)] == [200, 200, 429]
        # MCP traffic has its own buckets
        assert c.get("/healthz").status_code == 200


def test_updater_downloads_then_gets_304(served, tmp_path):
    local = tmp_path / "local" / "indec.duckdb"
    with app_client(served) as c:
        up = SnapshotUpdater(local, URL, client=c)
        assert up.due()
        assert up.check() == "updated"
        assert validate(local) == 6
        assert not up.due()  # checked just now
        assert up.check() == "unchanged"
    assert not list(local.parent.glob("*.download*"))  # temp files cleaned up


def test_updater_rejects_a_corrupt_download_and_keeps_the_old_file(served, tmp_path):
    local = tmp_path / "indec.duckdb"
    shutil.copy(served, local)
    before = local.read_bytes()
    bad = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=gzip.compress(b"not a duckdb file"), headers={"etag": '"x"'})))
    up = SnapshotUpdater(local, URL, client=bad)
    with pytest.raises(Exception):
        up.check()
    assert local.read_bytes() == before
    assert not list(tmp_path.glob("*.download*"))


def test_run_once_never_raises(tmp_path):
    down = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429)))
    up = SnapshotUpdater(tmp_path / "indec.duckdb", URL, client=down)
    up.run_once()  # logs a warning, keeps serving
    assert up.first_check.is_set() and not (tmp_path / "indec.duckdb").exists()


def test_pending_download_is_installed_at_start(served, tmp_path):
    local = tmp_path / "indec.duckdb"
    up = SnapshotUpdater(local, URL)
    shutil.copy(served, up.pending_path)
    assert up.apply_pending() and validate(local) == 6 and not up.pending_path.exists()


def test_updater_from_env_rules(monkeypatch, tmp_path):
    db = tmp_path / "indec.duckdb"
    monkeypatch.delenv("INDEC_DB", raising=False)
    monkeypatch.delenv("INDEC_SNAPSHOT_URL", raising=False)
    assert updater_from_env(db).url == DEFAULT_URL
    monkeypatch.setenv("INDEC_SNAPSHOT_URL", "")
    assert updater_from_env(db) is None  # opted out
    monkeypatch.delenv("INDEC_SNAPSHOT_URL")
    monkeypatch.setenv("INDEC_DB", str(db))
    assert updater_from_env(db) is None  # user manages their own file
    monkeypatch.setenv("INDEC_SNAPSHOT_URL", "https://example.test/x.gz")
    assert updater_from_env(db).url == "https://example.test/x.gz"


def test_default_db(monkeypatch, tmp_path):
    monkeypatch.setenv("INDEC_DB", str(tmp_path / "x.duckdb"))
    assert paths.default_db() == tmp_path / "x.duckdb"
    monkeypatch.delenv("INDEC_DB")
    assert paths.in_checkout()  # tests run from the repo
    assert paths.default_db() == Path(paths.__file__).resolve().parents[2] / "data" / "indec.duckdb"
    monkeypatch.setattr(paths, "in_checkout", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    if os.name == "posix" and not paths.sys.platform == "darwin":
        assert paths.default_db() == tmp_path / "indec-mcp" / "indec.duckdb"
