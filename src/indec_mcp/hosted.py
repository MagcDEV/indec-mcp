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

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .mirror import SOURCE, Limits, Mirror, QueryError
from .ratelimit import RateLimiter, RateLimitMiddleware, Rule
from .scheduler import SyncScheduler
from .server import create_server

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
    # Railway documents X-Real-IP as the client IP set by its edge (see docs/deploy.md). Its
    # X-Forwarded-For rightmost entry is an internal hop that varies per request, so it can't key
    # rate limits. Set CLIENT_IP_HEADER="" to fall back to X-Forwarded-For + TRUSTED_PROXY_HOPS.
    ip_header: str | None = "x-real-ip"
    proxy_hops: int = 1
    max_connections: int = 64
    sync_enabled: bool = True
    sync_hour_utc: int = 22

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = Path(__file__).resolve().parents[2] / "data" / "indec.duckdb"
        return cls(
            db_path=Path(os.environ.get("INDEC_DB") or default_db),
            port=int(os.environ.get("PORT", 8000)),
            per_ip_per_minute=_env_float("RATE_PER_IP_PER_MINUTE", cls.per_ip_per_minute),
            per_ip_burst=_env_float("RATE_PER_IP_BURST", cls.per_ip_burst),
            per_ip_per_day=_env_float("RATE_PER_IP_PER_DAY", cls.per_ip_per_day),
            global_per_second=_env_float("RATE_GLOBAL_PER_SECOND", cls.global_per_second),
            global_burst=_env_float("RATE_GLOBAL_BURST", cls.global_burst),
            ip_header=os.environ.get("CLIENT_IP_HEADER", cls.ip_header) or None,
            proxy_hops=int(os.environ.get("TRUSTED_PROXY_HOPS", cls.proxy_hops)),
            max_connections=int(os.environ.get("MAX_CONNECTIONS", cls.max_connections)),
            sync_enabled=os.environ.get("SYNC_ENABLED", "1") != "0",
            sync_hour_utc=int(os.environ.get("SYNC_HOUR_UTC", cls.sync_hour_utc)),
        )

    def limiter(self) -> RateLimiter:
        return RateLimiter(
            per_ip=[
                # Burst bucket: `per_ip_burst` at once, refilling at per_ip_per_minute/min.
                Rule(capacity=self.per_ip_burst, per_seconds=60 * self.per_ip_burst / self.per_ip_per_minute),
                Rule(capacity=self.per_ip_per_day, per_seconds=86_400),
            ],
            global_rule=Rule(capacity=self.global_burst, per_seconds=self.global_burst / self.global_per_second),
        )


def build_app(mirror: Mirror, settings: Settings, scheduler: SyncScheduler | None = None):
    server = create_server(mirror)

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
    return RateLimitMiddleware(
        app, settings.limiter(), path_prefix="/mcp", proxy_hops=settings.proxy_hops, ip_header=settings.ip_header
    )


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

    uvicorn.run(
        build_app(mirror, settings, scheduler),
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
