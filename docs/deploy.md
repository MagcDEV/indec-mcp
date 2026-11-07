# Deploying and operating the hosted MCP

Production: **https://indec-mcp-production.up.railway.app/mcp** (Streamable HTTP, no auth)

- Railway project `indec-mcp`, environment `production`, service `indec-mcp`
- One replica, built from `Dockerfile`, volume `indec-mcp-volume` mounted at `/data`
- Endpoints: `/mcp` (MCP), `/healthz` (status JSON, always 200), `/` (info and limits)

## Configuration (Railway service variables)

| Variable | Default | Notes |
|---|---|---|
| `INDEC_TOKEN` | — | **Secret.** INDEC JWT. Expires about 6 months after it's generated (current one: 2027-05-06). Without it, the daily sync is disabled. |
| `INDEC_DB` | `/data/indec.duckdb` | Set in the Dockerfile |
| `PORT` | set by Railway (8080) | The domain's target port must match |
| `CLIENT_IP_HEADER` | `x-real-ip` | The header that rate limits are keyed on (see below) |
| `RATE_PER_IP_PER_MINUTE` / `RATE_PER_IP_BURST` / `RATE_PER_IP_PER_DAY` | 30 / 20 / 2000 | Per-client limits |
| `RATE_GLOBAL_PER_SECOND` / `RATE_GLOBAL_BURST` | 20 / 100 | Total for the whole instance |
| `MAX_CONNECTIONS` | 64 | uvicorn returns 503 beyond this |
| `SYNC_ENABLED` / `SYNC_HOUR_UTC` | 1 / 22 | Daily sync at 22:00 UTC (19:00 ART) |
| `STATS_TOKEN` | — | **Secret.** Enables `GET /stats` (Bearer token). Without it, `/stats` returns 404. |
| `USAGE_ENABLED` / `USAGE_RETENTION_DAYS` | 1 / 90 | Anonymous usage analytics (see below) |

Railway's legacy config-as-code (`railway.toml`) is not used: new services can't opt into it, and it stops being read on 2026-12-01. Settings live on the service itself. They could be moved to Railway IaC (`.railway/railway.py`) later.

## Abuse protection

Requests pass through these layers in order:

1. **Railway edge.** TLS termination, and it sets `X-Real-IP`.
2. **uvicorn.** At most `MAX_CONNECTIONS` concurrent connections (503 beyond that). Keep-alive is 5 s.
3. **`RateLimitMiddleware`.** Token buckets per client IP for bursts and for the day, plus one global bucket. Over the limit, the response is `429` with `Retry-After` and a JSON-RPC error body. Rate-limit events are logged with a hashed IP. There are no access logs.
4. **Request size.** Bodies over 64 KB get `413`.
5. **`Mirror`.**
   - The database is read-only, with file and network access disabled and the configuration locked.
   - Queries are SELECT only, with a 5 s timeout and at most 500 rows.
   - At most 4 queries run at once. Callers beyond that wait up to 3 s, then get "servidor ocupado" (server busy).

The INDEC API is never called on behalf of users. Only the daily sync calls it, with about 2,000 requests per day whatever the traffic. No LLM runs on the server, so each request costs CPU time only.

### Client IP on Railway

Railway documents `X-Real-IP` as the client-IP header ([specs & limits](https://docs.railway.com/networking/public-networking/specs-and-limits)). The rightmost `X-Forwarded-For` entry varies between requests, so keying rate limits on it would give every request a fresh bucket. This was observed in production on 2026-11-07, with 25 back-to-back requests and no 429.

**Verified in production on 2026-11-07, after switching to `X-Real-IP`:**

| Test | Result |
|---|---|
| 25 requests, each with a *different forged* `X-Real-IP` | 24×200, then 429 (burst of 20 plus refill). Railway overwrites the header, so forged values don't create new buckets. |
| Immediately after, forged `X-Forwarded-For` | 429 (same bucket) |
| `/healthz` while rate limited | 200. Health and landing pages aren't limited. |
| 429 response | Has `Retry-After` and a JSON-RPC error body |

Re-run this check if a CDN or another proxy is ever put in front of the service.

## Usage analytics

The server records one row per MCP request in `/data/usage.sqlite`, on the same volume as the mirror. Rows older than `USAGE_RETENTION_DAYS` are deleted.

| Stored | Not stored |
|---|---|
| `user`: HMAC-SHA256 of the client IP with a secret salt (kept in `/data/usage.salt`), truncated to 16 hex characters | The IP address itself |
| `client`: the client's self-reported `clientInfo.name` (or the User-Agent product token) | Conversations or prompts |
| `method`, `tool`, `status`, `is_error`, `ms` | SQL text (only the referenced table names) |
| `detail`: a coarse topic built from structured arguments (for example `Núcleo@GBA` or `exportaciones\|China\|anio`) | Free text, except `trade_codes` queries (clipped to 30 characters) |

Writes are batched by a background thread, so requests never wait on disk. Rate-limited (429) requests are recorded too, because the usage middleware sits outside the rate limiter.

To read the report:

```bash
indec-stats                 # pretty report for the last 30 days (STATS_TOKEN from env or .env)
indec-stats --days 7 --json # raw JSON
curl -H "Authorization: Bearer $STATS_TOKEN" https://indec-mcp-production.up.railway.app/stats?days=30
```

It shows users today, over 7 days and over 30 days, new vs. returning users, a daily breakdown, clients (apps), tools (calls, users, error %, latency), the top topics, and 429s.

**What "users" means:** distinct client IPs, after hashing. Several people behind one network (an office, a mobile carrier's NAT) count as one, and one person whose IP changes counts as several. Treat the number as an estimate. Losing `usage.salt` resets every user ID.

## Routine operations

```bash
railway logs --service indec-mcp --lines 200          # sync results, rate-limit events
curl -s https://indec-mcp-production.up.railway.app/healthz
railway up --detach --service indec-mcp -m "..."      # redeploy from this directory
```

- **Token rotation**, before 2027-05-06: generate a new token at api.indec.gob.ar, then run `railway variable set INDEC_TOKEN --stdin --service indec-mcp`.
- **A dataset shows as `stale` in `/healthz`**: its last sync failed and the previous data is being served. Check the logs. The next daily run retries it.
- **Redeploying while a sync runs** kills the sync. The next boot syncs again only if the mirror is missing or older than 26 h.
