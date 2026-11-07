"""Hosted entry point: MCP over Streamable HTTP with abuse protection and a daily sync.

Run: `indec-mcp-http` (reads PORT, INDEC_DB, INDEC_TOKEN and the limits below from env).

Abuse protection, outermost first:
1. uvicorn `limit_concurrency`: connections beyond it get 503 immediately;
2. RateLimitMiddleware: per-IP burst and daily buckets plus a global bucket -> 429;
3. request bodies capped at 64 KB (SQL queries are small);
4. Mirror: read-only sandboxed DuckDB, max 4 concurrent queries, 5 s timeout, 500-row cap.
No LLM runs here (clients bring their own), so cost per request is CPU only.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .mirror import SOURCE, Limits, Mirror, QueryError
from .paths import default_db
from .providers import ProviderRanges
from .ratelimit import RateLimiter, RateLimitMiddleware, Rule
from .scheduler import SyncScheduler
from .server import create_server
from .snapshot import SnapshotPublisher
from .usage import UsageMiddleware, UsageRecorder

log = logging.getLogger("indec_mcp.http")

MAX_BODY_BYTES = 64 * 1024


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    port: int = 8000
    per_ip_per_minute: float = 30  # sustained; a chat turn is ~3-10 tool calls
    per_ip_burst: float = 20
    per_ip_per_day: float = 2000
    global_per_second: float = 20
    global_burst: float = 100
    # Claude.ai / ChatGPT call from shared provider IPs (one user arrived from 6 Anthropic IPs), so
    # each provider gets one bucket for all its users, sized well above a single person's.
    provider_burst: float = 200
    provider_per_minute: float = 600
    provider_per_day: float = 100_000
    # Railway overwrites X-Real-IP with the client IP (documented, and verified in production, see docs/deploy.md). Its
    # X-Forwarded-For rightmost entry is an internal hop that varies per request, so it can't key
    # rate limits. Set CLIENT_IP_HEADER="" to fall back to X-Forwarded-For + TRUSTED_PROXY_HOPS.
    ip_header: str | None = "x-real-ip"
    proxy_hops: int = 1
    max_connections: int = 64
    sync_enabled: bool = True
    sync_hour_utc: int = 22
    usage_enabled: bool = True
    usage_retention_days: int = 90
    stats_token: str | None = None  # enables GET /stats; unset -> 404
    # GET /snapshot/indec.duckdb.gz (~12 MB) feeds local installs. A local server checks every 6h
    # and mostly gets a 304, so these only bite on abuse; the global cap bounds egress.
    snapshot_enabled: bool = True
    snapshot_per_ip_per_hour: float = 6
    snapshot_per_ip_per_day: float = 30
    snapshot_global_per_hour: float = 120

    @property
    def usage_db(self) -> Path:
        return self.db_path.parent / "usage.sqlite"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=default_db(),
            port=int(os.environ.get("PORT", 8000)),
            per_ip_per_minute=_env_float("RATE_PER_IP_PER_MINUTE", cls.per_ip_per_minute),
            per_ip_burst=_env_float("RATE_PER_IP_BURST", cls.per_ip_burst),
            per_ip_per_day=_env_float("RATE_PER_IP_PER_DAY", cls.per_ip_per_day),
            global_per_second=_env_float("RATE_GLOBAL_PER_SECOND", cls.global_per_second),
            global_burst=_env_float("RATE_GLOBAL_BURST", cls.global_burst),
            provider_burst=_env_float("RATE_PROVIDER_BURST", cls.provider_burst),
            provider_per_minute=_env_float("RATE_PROVIDER_PER_MINUTE", cls.provider_per_minute),
            provider_per_day=_env_float("RATE_PROVIDER_PER_DAY", cls.provider_per_day),
            ip_header=os.environ.get("CLIENT_IP_HEADER", cls.ip_header) or None,
            proxy_hops=int(os.environ.get("TRUSTED_PROXY_HOPS", cls.proxy_hops)),
            max_connections=int(os.environ.get("MAX_CONNECTIONS", cls.max_connections)),
            sync_enabled=os.environ.get("SYNC_ENABLED", "1") != "0",
            sync_hour_utc=int(os.environ.get("SYNC_HOUR_UTC", cls.sync_hour_utc)),
            usage_enabled=os.environ.get("USAGE_ENABLED", "1") != "0",
            usage_retention_days=int(os.environ.get("USAGE_RETENTION_DAYS", cls.usage_retention_days)),
            stats_token=os.environ.get("STATS_TOKEN") or None,
            snapshot_enabled=os.environ.get("SNAPSHOT_ENABLED", "1") != "0",
        )

    def limiter(self) -> RateLimiter:
        return RateLimiter(
            per_ip=[
                # Burst bucket: `per_ip_burst` at once, refilling at per_ip_per_minute/min.
                Rule(capacity=self.per_ip_burst, per_seconds=60 * self.per_ip_burst / self.per_ip_per_minute),
                Rule(capacity=self.per_ip_per_day, per_seconds=86_400),
            ],
            global_rule=Rule(capacity=self.global_burst, per_seconds=self.global_burst / self.global_per_second),
            profiles={"provider": [
                Rule(capacity=self.provider_burst, per_seconds=60 * self.provider_burst / self.provider_per_minute),
                Rule(capacity=self.provider_per_day, per_seconds=86_400),
            ]},
        )

    def snapshot_limiter(self) -> RateLimiter:
        return RateLimiter(
            per_ip=[
                Rule(capacity=self.snapshot_per_ip_per_hour, per_seconds=3600),
                Rule(capacity=self.snapshot_per_ip_per_day, per_seconds=86_400),
            ],
            global_rule=Rule(capacity=self.snapshot_global_per_hour, per_seconds=3600),
        )


def build_app(
    mirror: Mirror,
    settings: Settings,
    scheduler: SyncScheduler | None = None,
    usage: UsageRecorder | None = None,
    providers: ProviderRanges | None = None,
):
    server = create_server(mirror)
    snapshots = SnapshotPublisher(settings.db_path) if settings.snapshot_enabled else None

    @server.custom_route("/snapshot/indec.duckdb.gz", methods=["GET", "HEAD"], include_in_schema=False)
    async def snapshot(request: Request) -> Response:
        # The whole mirror (public INDEC data only), gzipped, for `indec-mcp` running locally.
        if snapshots is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        import anyio

        try:
            path, etag = await anyio.to_thread.run_sync(snapshots.current)
        except FileNotFoundError:
            return JSONResponse({"error": "mirror not ready"}, status_code=503, headers={"retry-after": "600"})
        tag = f'"{etag}"'
        if tag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers={"etag": tag})
        log.info("snapshot download %s", etag)
        return FileResponse(path, media_type="application/gzip", filename="indec.duckdb.gz",
                            headers={"etag": tag, "cache-control": "public, max-age=3600"})

    @server.custom_route("/stats", methods=["GET"], include_in_schema=False)
    async def stats(request: Request) -> JSONResponse:
        # Hidden unless STATS_TOKEN is configured; constant-time token check.
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not settings.stats_token or usage is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not hmac.compare_digest(given.encode(), settings.stats_token.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            days = int(request.query_params.get("days", 30))
        except ValueError:
            days = 30
        return JSONResponse(usage.report(days))

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        # Always 200 so the platform keeps the service up while the first sync runs.
        try:
            datasets = mirror.datasets()
            ready = True
        except QueryError:
            datasets, ready = [], False
        return JSONResponse({
            "status": "ok",
            "mirror_ready": ready,
            "datasets": len(datasets),
            "stale_or_failed": [d["table_name"] for d in datasets if d["status"] != "ok"],
            "synced_at": max((d["synced_at"] for d in datasets), default=None),
            "last_sync": scheduler.last_result if scheduler else None,
            "cache": mirror.cache.stats(),
            "provider_ranges": providers.counts() if providers else None,
        })

    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    async def index(request: Request) -> JSONResponse:
        return JSONResponse({
            "name": "INDEC MCP (no oficial)",
            "mcp_endpoint": f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://"
                            f"{request.headers.get('host', request.url.netloc)}/mcp",
            "how_to_connect": "Agregá esta URL como servidor MCP remoto (Streamable HTTP) en tu cliente: "
                              "Claude, ChatGPT, Cursor, etc. No requiere registro.",
            "limits": {
                "per_ip_per_minute": settings.per_ip_per_minute,
                "per_ip_per_day": settings.per_ip_per_day,
                "sql_timeout_s": mirror.limits.timeout_s,
                "max_rows": mirror.limits.max_rows,
            },
            "source": SOURCE,
            "disclaimer": "Servicio no oficial, no afiliado al INDEC. Datos: Fuente INDEC.",
        })

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # no server-side sessions to exhaust; any instance can serve any request
        json_response=True,
        max_request_body_size=MAX_BODY_BYTES,
        # DNS-rebinding protection guards servers on localhost from malicious web pages. This one is
        # public and unauthenticated by design, so Host/Origin checks add nothing here.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    classify = providers.classify if providers else None
    app = RateLimitMiddleware(
        app, settings.limiter(), path_prefix="/mcp", proxy_hops=settings.proxy_hops, ip_header=settings.ip_header,
        classify=classify,
    )
    app = RateLimitMiddleware(
        app, settings.snapshot_limiter(), path_prefix="/snapshot", proxy_hops=settings.proxy_hops,
        ip_header=settings.ip_header,
    )
    if usage is not None:  # outermost, so rate-limited (429) requests are counted too
        app = UsageMiddleware(app, usage, path_prefix="/mcp", proxy_hops=settings.proxy_hops,
                              ip_header=settings.ip_header, classify=classify)
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Stateless mode logs "Terminating session: None" on every request.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    settings = Settings.from_env()
    mirror = Mirror(settings.db_path, Limits())

    scheduler = None
    if settings.sync_enabled and os.environ.get("INDEC_TOKEN"):
        scheduler = SyncScheduler(
            settings.db_path, hour_utc=settings.sync_hour_utc, work_dir=settings.db_path.parent
        )
        scheduler.start()
    else:
        log.warning("daily sync disabled (SYNC_ENABLED=0 or INDEC_TOKEN missing); serving existing mirror")

    usage = None
    if settings.usage_enabled:
        usage = UsageRecorder(settings.usage_db, retention_days=settings.usage_retention_days)
        log.info("usage analytics on (%s, %sd retention, /stats %s)", settings.usage_db.name,
                 settings.usage_retention_days, "enabled" if settings.stats_token else "disabled")

    providers = ProviderRanges(cache_dir=settings.db_path.parent / "provider_ranges")
    providers.start_daily_refresh()
    log.info("provider ranges loaded: %s", providers.counts())

    uvicorn.run(
        build_app(mirror, settings, scheduler, usage, providers),
        host="0.0.0.0",
        port=settings.port,
        proxy_headers=False,  # we read X-Forwarded-For ourselves, counting trusted hops
        server_header=False,
        access_log=False,  # don't log client IPs; rate-limit events log a hash
        limit_concurrency=settings.max_connections,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
