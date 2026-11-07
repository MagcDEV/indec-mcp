# indec-mcp (working name)

An AI and tooling layer over the official **[Catálogo API-INDEC](https://api.indec.gob.ar)**, the API that Argentina's national statistics institute launched on 2026-11-02 (in beta). The goal is to let anyone "chat with INDEC data" (inflation, basic baskets, wages, economic activity, poverty, employment, trade, population) from an AI assistant, with correct figures and proper citations.

> Unofficial. Not affiliated with INDEC. Data: **Fuente: INDEC**.

## Status

- ✅ Research ([docs/api-research.md](docs/api-research.md))
- ✅ **Sync job**: mirrors all 19 datasets (about 2 M rows) into one DuckDB file
- ✅ **Local MCP server** (stdio) over the mirror, tested end to end with Claude Code
- ✅ **Hosted MCP**: `https://indec-mcp-production.up.railway.app/mcp`, with rate limits and a daily sync ([docs/deploy.md](docs/deploy.md))

## Connect (hosted, no signup)

Add `https://indec-mcp-production.up.railway.app/mcp` as a remote MCP server (Streamable HTTP):

```bash
claude mcp add --transport http indec https://indec-mcp-production.up.railway.app/mcp   # Claude Code
```

In Claude.ai, ChatGPT, Cursor and similar clients, add a custom connector or remote MCP server with the same URL.

**Limits per client IP:** a burst of 20 requests, 30 per minute sustained, and 2000 per day. Over the limit you get HTTP 429 with `Retry-After`. The data syncs daily at 19:00 ART. Operations are covered in [docs/deploy.md](docs/deploy.md).

## MCP server

The server has six read-only tools:

| Tool | Use it for |
|---|---|
| `latest_indicators` | Headline figures, each with its period: CPI (general and core), EMAE, wages, CBA/CBT, poverty, unemployment |
| `inflation_series` | A CPI series by category (general, core, regulated, divisions…) and region, with monthly, year-on-year and year-to-date changes, the index, or incidence |
| `compare_series` | Two to four monthly series side by side, aligned on their shared months: wages vs CPI (real wages), basic basket vs wages, CPI by region, EMAE. Shows INDEC's published changes, plus a cumulative summary that is marked as computed |
| `list_datasets` | What's in the mirror, with its freshness |
| `describe_dataset` | Columns, their meaning, the **exact values** of each text dimension (filters are case-sensitive) and sample rows |
| `query_sql` | Read-only DuckDB SQL for everything else |

The server's instructions tell the model to cite "Fuente: INDEC" with the period of each figure, to flag provisional data, to prefer INDEC's published changes over computing its own, and to say when it has computed something.

**`query_sql` sandbox:**
- The database is opened read-only.
- `enable_external_access=false` means no file access, network access, `ATTACH` or extension installs.
- The configuration is locked, so a query can't turn those settings back on.
- Each call accepts one SELECT statement, runs for at most 5 s and returns at most 500 rows.
- Tests cover `DROP`, `INSERT`, `COPY TO`, `read_csv('/etc/passwd')`, URLs and runaway joins.

**Use it from Claude Code.** `.mcp.json` registers the server for this project. Start `claude` in this folder and approve the `indec` server when asked. Then ask things like *"¿Cómo viene la inflación de alimentos en el NOA vs. GBA este año?"*

For other MCP clients, run `.venv/bin/indec-mcp`. It speaks stdio and reads `INDEC_DB`, which defaults to `data/indec.duckdb`.

## Sync

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # or: uv sync
echo 'INDEC_TOKEN=...' > .env                                # git ignores this file

.venv/bin/indec-sync                       # full refresh -> data/indec.duckdb (about 15 min, about 2,000 requests)
.venv/bin/indec-sync --only ipc_nacional   # refresh just one dataset; the rest are carried over
.venv/bin/python -m pytest                 # 78 tests: fake PostgREST server and fixture DB, no network
```

How it works:
- **Full refresh every run.** INDEC revises provisional figures, so appending only new periods would keep outdated values.
- **Stable pagination.** Each table is ordered by its full primary key, so pages never skip or repeat rows.
- **No half-written files.** Each run builds `indec.duckdb.tmp` and then replaces the old file in one step.
- **Partial failures are survivable.** If one dataset fails, its previous table is kept and marked `stale`. An expired token aborts the run and leaves the mirror untouched.
- **Normalized names.** All column names are lowercase.
- **Built-in validation.** Row counts are checked against the API's reported total, and duplicate primary keys are flagged.
- **Metadata for the MCP layer.** The `_datasets` table holds descriptions, primary keys, row counts, latest period and status. The `_columns` table holds each column's type and its description from the API spec.

```sql
-- duckdb data/indec.duckdb
SELECT table_name, row_count, latest_periodo, status FROM _datasets ORDER BY row_count DESC;
```

- [docs/api-research.md](docs/api-research.md): how the API works (auth, PostgREST queries, limits, data quirks, terms) and what is still unknown
- [docs/datasets.md](docs/datasets.md): all 19 datasets with their columns and primary keys (generated from the spec)
- [docs/ideas.md](docs/ideas.md): project ideas, the recommended architecture for a hosted MCP that resists abuse, and a roadmap
- [spec/indec-openapi.json](spec/indec-openapi.json): a snapshot of the public OpenAPI 3.1 spec

## Other scripts

```bash
scripts/fetch-spec.sh                 # refresh the spec snapshot and regenerate docs/datasets.md (no token needed)
INDEC_TOKEN=... scripts/probe.sh      # row counts, latency and rate-limit headers for each dataset
```

To get a token, sign up at <https://api.indec.gob.ar>, verify with the OTP, and generate it from your profile. It is shown once. The FAQ says it lasts 12 months, but the one we got expires after 6. Keep it in `.env`, which git ignores; `.env.example` shows the format.
