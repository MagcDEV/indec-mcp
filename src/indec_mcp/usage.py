"""Privacy-preserving usage analytics for the hosted MCP server.

What is stored per MCP request (SQLite on the volume, kept `retention_days`):
- `user`: HMAC-SHA256(secret salt, client IP), truncated. The IP itself is never stored, and the
  salt never leaves the server, so the ID can't be reversed or joined with other datasets.
- `client`: the MCP client's self-reported name (clientInfo), else the User-Agent product token.
- `method`, `tool`, `status`, `is_error`, `ms`.
- `detail`: a coarse topic built from *structured* tool arguments (e.g. "Alimentos@GBA", "China").
  Free text is never stored: for `query_sql` only the referenced table names are kept.

Writes go through an in-memory queue flushed by a background thread, so requests never wait
on disk. If anything here fails, it's logged and the request proceeds untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .ratelimit import client_ip

log = logging.getLogger("indec_mcp.usage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts       INTEGER NOT NULL,   -- unix seconds
    day      TEXT    NOT NULL,   -- UTC date, YYYY-MM-DD
    user     TEXT    NOT NULL,   -- salted hash of the client IP
    client   TEXT,
    method   TEXT,
    tool     TEXT,
    detail   TEXT,
    status   INTEGER,
    is_error INTEGER,
    ms       INTEGER,
    via      TEXT                -- 'anthropic' / 'openai' for hosted AI clients, NULL if direct
);
CREATE INDEX IF NOT EXISTS events_day ON events(day);
CREATE INDEX IF NOT EXISTS events_user ON events(user, day);
"""

_MAX_BODY = 64 * 1024
_TABLE_REF = re.compile(r'\b(?:from|join)\s+"?([a-z_][a-z0-9_]*)"?', re.IGNORECASE)


def _clip(v: Any, n: int = 40) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_detail(tool: str | None, args: dict | None) -> str | None:
    """Coarse, non-free-text summary of what a tool call was about."""
    a = args or {}
    try:
        if tool == "inflation_series":
            return f"{_clip(a.get('category') or 'Nivel general')}@{_clip(a.get('region') or 'Nacional', 20)}"
        if tool == "compare_series":
            return _clip(",".join(map(str, a.get("series") or [])), 120)
        if tool == "foreign_trade":
            parts = [a.get("flow") or "exportaciones", a.get("country"), a.get("category"), a.get("group_by")]
            return "|".join(_clip(p, 30) for p in parts if p)
        if tool == "population":
            return "|".join(_clip(p, 30) for p in (a.get("place") or "Argentina", a.get("by")) if p)
        if tool == "describe_dataset":
            return _clip(a.get("table") or "", 60)
        if tool == "trade_codes":
            return _clip(a.get("query") or "", 30)
        if tool == "query_sql":
            tables = sorted({t.lower() for t in _TABLE_REF.findall(str(a.get("sql") or ""))})
            return "tables:" + ",".join(tables)[:110] if tables else "tables:?"
    except Exception:  # noqa: BLE001 - never let analytics break a request
        return None
    return None


def _client_name(params: dict | None, headers: dict[str, str]) -> tuple[str | None, bool]:
    """(name, explicit): explicit clientInfo wins over the User-Agent product token."""
    p = params or {}
    info = p.get("clientInfo") or (p.get("_meta") or {}).get("io.modelcontextprotocol/clientInfo")
    if isinstance(info, dict) and info.get("name"):
        return _clip(str(info["name"]), 40), True
    ua = headers.get("user-agent", "")
    return (_clip(ua.split("/")[0].split(" ")[0], 40) if ua else None), False


class UsageRecorder:
    def __init__(self, db_path: Path, *, salt: bytes | None = None, retention_days: int = 90,
                 flush_interval: float = 5.0, start_thread: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.salt = salt or self._load_or_create_salt()
        self.retention_days = retention_days
        self.flush_interval = flush_interval
        self._q: queue.Queue[tuple] = queue.Queue(maxsize=50_000)
        self._clients: dict[str, str] = {}  # user -> last self-reported client name (in memory only)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
            if "via" not in cols:  # migrate v1 tables; Claude.ai traffic predates classification
                con.execute("ALTER TABLE events ADD COLUMN via TEXT")
                con.execute("UPDATE events SET via = 'anthropic' WHERE client LIKE 'Anthropic%'")
        self._stop = threading.Event()
        self._last_prune = 0.0
        if start_thread:
            threading.Thread(target=self._run, name="usage-writer", daemon=True).start()

    # -- identity --------------------------------------------------------------------------------

    def _load_or_create_salt(self) -> bytes:
        f = self.db_path.with_suffix(".salt")
        if f.exists():
            return f.read_bytes()
        salt = secrets.token_bytes(32)
        f.write_bytes(salt)
        f.chmod(0o600)
        return salt

    def user_id(self, ip: str) -> str:
        return hmac.new(self.salt, ip.encode(), hashlib.sha256).hexdigest()[:16]

    # -- writing ---------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def record(self, *, user: str, client: str | None, method: str | None, tool: str | None,
               detail: str | None, status: int, is_error: bool, ms: int, client_explicit: bool = True,
               via: str | None = None) -> None:
        # Priority: self-reported clientInfo now > clientInfo this user sent earlier > User-Agent.
        if client and client_explicit:
            if len(self._clients) > 100_000:
                self._clients.clear()
            self._clients[user] = client
        else:
            client = self._clients.get(user) or client
        now = int(time.time())
        row = (now, time.strftime("%Y-%m-%d", time.gmtime(now)), user, client, method, tool, detail,
               status, int(is_error), ms, via)
        try:
            self._q.put_nowait(row)
        except queue.Full:
            log.warning("usage queue full; dropping event")

    def flush(self) -> int:
        rows = []
        while True:
            try:
                rows.append(self._q.get_nowait())
            except queue.Empty:
                break
        if rows:
            with self._connect() as con:
                con.executemany(
                    "INSERT INTO events (ts, day, user, client, method, tool, detail, status, is_error, ms, via) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        if time.time() - self._last_prune > 3600:
            self.prune()
        return len(rows)

    def prune(self) -> None:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - self.retention_days * 86400))
        with self._connect() as con:
            con.execute("DELETE FROM events WHERE day < ?", (cutoff,))
        self._last_prune = time.time()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("usage flush failed")

    def close(self) -> None:
        self._stop.set()
        self.flush()

    # -- reading ---------------------------------------------------------------------------------

    def report(self, days: int = 30) -> dict:
        self.flush()
        days = max(1, min(int(days), self.retention_days))
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
        con = self._connect()
        con.row_factory = sqlite3.Row

        def q(sql: str, *params) -> list[dict]:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            def uniq(n: int) -> int:
                d = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (n - 1) * 86400))
                return con.execute("SELECT count(DISTINCT user) FROM events WHERE day >= ? AND via IS NULL",
                                   (d,)).fetchone()[0]

            first_seen = "(SELECT user, min(day) AS first FROM events WHERE via IS NULL GROUP BY user)"
            return {
                "window_days": days,
                "users": {
                    "today": uniq(1), "last_7_days": uniq(7), "last_30_days": uniq(30),
                    "all_time_retained": con.execute(
                        "SELECT count(DISTINCT user) FROM events WHERE via IS NULL").fetchone()[0],
                    "note": "Usuarios directos aproximados: IPs distintas (cifradas), sin contar Claude.ai/ChatGPT. "
                            "Una red compartida cuenta como uno; una IP que cambia puede contar como varios.",
                },
                "via_providers": q(
                    "SELECT via, count(*) AS requests, count(*) FILTER (WHERE method = 'tools/call') AS tool_calls, "
                    "count(DISTINCT day) AS active_days, count(*) FILTER (WHERE status = 429) AS rate_limited "
                    "FROM events WHERE day >= ? AND via IS NOT NULL GROUP BY via ORDER BY requests DESC", since,
                ),
                "daily": q(
                    f"SELECT e.day, count(DISTINCT CASE WHEN e.via IS NULL THEN e.user END) AS users, "
                    f"count(DISTINCT CASE WHEN f.first = e.day THEN e.user END) AS new_users, "
                    f"count(*) FILTER (WHERE e.via IS NOT NULL) AS via_provider, "
                    f"count(*) FILTER (WHERE e.method = 'tools/call') AS tool_calls, "
                    f"count(*) FILTER (WHERE e.status = 429) AS rate_limited, count(*) AS requests "
                    f"FROM events e LEFT JOIN {first_seen} f USING (user) WHERE e.day >= ? GROUP BY e.day ORDER BY e.day",
                    since,
                ),
                "clients": q(
                    "SELECT coalesce(client, '?') AS client, count(DISTINCT user) AS users, count(*) AS requests "
                    "FROM events WHERE day >= ? GROUP BY 1 ORDER BY users DESC, requests DESC LIMIT 20", since,
                ),
                "tools": q(
                    "SELECT tool, count(*) AS calls, count(DISTINCT user) AS users, "
                    "round(100.0 * sum(is_error) / count(*), 1) AS error_pct, "
                    "CAST(avg(ms) AS INTEGER) AS avg_ms, max(ms) AS max_ms "
                    "FROM events WHERE day >= ? AND method = 'tools/call' GROUP BY tool ORDER BY calls DESC", since,
                ),
                "top_topics": q(
                    "SELECT tool, detail, count(*) AS calls FROM events "
                    "WHERE day >= ? AND method = 'tools/call' AND detail IS NOT NULL "
                    "GROUP BY tool, detail ORDER BY calls DESC LIMIT 25", since,
                ),
                "methods": q(
                    "SELECT coalesce(method, '?') AS method, count(*) AS requests FROM events WHERE day >= ? "
                    "GROUP BY 1 ORDER BY requests DESC", since,
                ),
                "retention": q(
                    f"SELECT count(*) FILTER (WHERE n_days = 1) AS one_day_users, "
                    f"count(*) FILTER (WHERE n_days >= 2) AS returning_users "
                    f"FROM (SELECT user, count(DISTINCT day) AS n_days FROM events WHERE day >= ? AND via IS NULL "
                    f"GROUP BY user)",
                    since,
                )[0],
                "rate_limited_requests": con.execute(
                    "SELECT count(*) FROM events WHERE day >= ? AND status = 429", (since,)).fetchone()[0],
                "generated_for": today,
            }
        finally:
            con.close()


class UsageMiddleware:
    """Outermost ASGI middleware: sees every /mcp request, including ones rejected with 429."""

    def __init__(self, app, recorder: UsageRecorder, *, path_prefix: str = "/mcp", proxy_hops: int = 1,
                 ip_header: str | None = None, classify=None):
        self.app = app
        self.classify = classify
        self.recorder = recorder
        self.path_prefix = path_prefix
        self.proxy_hops = proxy_hops
        self.ip_header = ip_header

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.path_prefix) or scope["method"] != "POST":
            return await self.app(scope, receive, send)

        # Buffer the request body (up to _MAX_BODY) to read the JSON-RPC envelope, then replay the
        # exact same messages to the app. Past the cap we stop buffering and pass the rest through,
        # so the app's own size limit still answers 413.
        buffered, size = [], 0
        while True:
            msg = await receive()
            buffered.append(msg)
            if msg["type"] != "http.request":
                break
            size += len(msg.get("body", b""))
            if not msg.get("more_body", False) or size > _MAX_BODY:
                break
        raw = b"".join(m.get("body", b"") for m in buffered) if size <= _MAX_BODY else b""
        pending = list(buffered)

        async def replay():
            if pending:
                return pending.pop(0)
            return await receive()

        status, resp_parts = 500, []

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and sum(map(len, resp_parts)) < 256 * 1024:
                resp_parts.append(message.get("body", b""))
            await send(message)

        t0 = time.perf_counter()
        try:
            await self.app(scope, replay, capture)
        finally:
            try:
                self._record(scope, raw, status, b"".join(resp_parts), int((time.perf_counter() - t0) * 1000))
            except Exception:  # noqa: BLE001
                log.exception("usage record failed")

    def _record(self, scope, raw: bytes, status: int, resp: bytes, ms: int) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            msg = json.loads(raw) if raw else {}
        except ValueError:
            msg = {}
        if isinstance(msg, list):  # JSON-RPC batch: record the first message
            msg = msg[0] if msg else {}
        method = msg.get("method") if isinstance(msg, dict) else None
        params = msg.get("params") if isinstance(msg, dict) else None
        tool = detail = None
        if method == "tools/call" and isinstance(params, dict):
            tool = _clip(str(params.get("name") or ""), 40)
            detail = tool_detail(tool, params.get("arguments"))
        is_error = status >= 400
        if not is_error and method == "tools/call" and resp:
            try:
                result = json.loads(resp).get("result") or {}
                is_error = bool(result.get("isError"))
            except ValueError:
                pass
        ip = client_ip(scope, self.proxy_hops, self.ip_header)
        client, explicit = _client_name(params if isinstance(params, dict) else None, headers)
        via = self.classify(ip) if self.classify else None
        self.recorder.record(
            via=via, user=self.recorder.user_id(ip), client=client, client_explicit=explicit,
            method=_clip(method, 40) if method else None, tool=tool, detail=detail,
            status=status, is_error=is_error, ms=ms,
        )
