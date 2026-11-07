# Development and self-hosting

This page is for running the server yourself or changing its code. To use the hosted server, see the [README](../README.md).

## Architecture

```
INDEC API (api.indec.gob.ar, PostgREST, JWT)
      │  once a day: ~2,000 requests, ~20 min
      ▼
indec-sync ──► data/indec.duckdb   (19 tables, ~2 M rows, ~32 MB, + _datasets/_columns metadata)
                     │  read-only, sandboxed
                     ▼
              Mirror (mirror.py, topics.py)
                     │
              MCP server (server.py) ──► stdio (indec-mcp)  or  Streamable HTTP (indec-mcp-http)
```

| Module | Role |
|---|---|
| `catalog.py` | Parses INDEC's OpenAPI spec: datasets, primary keys, column descriptions |
| `client.py` | HTTP client: auth, pagination by full primary key, retries with backoff |
| `sync.py` | `indec-sync`: full refresh into DuckDB, validation, atomic swap |
| `mirror.py` | Sandboxed read-only queries; IPC, snapshot and series comparison logic |
| `topics.py` | Foreign trade (with code lookup and confidential-row reporting) and population |
| `indices.py` | One shared engine with a per-dataset spec: wholesale prices, construction costs, industrial production, wages and EMAE |
| `living.py` | Basic and child-raising baskets, poverty and the labor market |
| `server.py` | MCP tools, prompts and resources (`indec-mcp`, stdio) |
| `hosted.py`, `ratelimit.py`, `scheduler.py` | `indec-mcp-http`: HTTP transport, rate limits, daily sync |

## Tools, prompts and resources

| Tool | What it does |
|---|---|
| `latest_indicators` | Headline figures, each with its period: CPI (general and core), EMAE, wages, CBA/CBT, poverty, unemployment |
| `inflation_series` | CPI by category (general, core, regulated, divisions…) and region: monthly, year-on-year and year-to-date changes, the index, or incidence |
| `compare_series` | 2–4 monthly series (`ipc[:cat][@region]`, `salarios[:sector]`, `cbt`, `cba`, `emae`, `ipim`/`ipib`/`ipp`, `icc[:cat]`, `ipi[:branch]`, `ipi_minero`, `emae:sector`) aligned on their shared months. Shows published changes, or levels rebased to 100, plus a summary that is marked as computed (for example real wages) |
| `foreign_trade` | Exports and imports since 1986 by month, year, country or category, with names instead of codes. Reports the confidential rows the sums leave out |
| `trade_codes` | Search the trade code dictionary, ignoring accents |
| `population` | Country, province or department; by sex, age group or department; 2022–2040 |
| `wholesale_prices` | IPIM, IPIB and IPP (since Dec 2015) for the overall level, domestic vs. imported, sector or CIIU product |
| `construction_costs` | ICC for GBA (since Nov 2015): overall level, materials, labour, general expenses, work items and products |
| `wages` | Wage index by sector (registered, unregistered, private, public): monthly, year-on-year and year-to-date changes, plus the index (base Oct-2016) |
| `economic_activity` | EMAE overall level (original, seasonally adjusted and trend series) or by one of 16 sectors (original series only) |
| `basic_baskets` | CBT/CBA in pesos per adult equivalent and for INDEC's 3 household types (GBA), or the child-raising basket by age group |
| `poverty` / `labor_market` | EPH, 31 urban areas: poverty and indigence (half-yearly), and activity, employment, unemployment, formality and underemployment (quarterly) |
| `industrial_production` | IPI manufacturing (since 2016, by division and branch) and mining (since 2017). `v_ia`/`v_acum` come from the original series and `v_m` from the seasonally adjusted one |
| `list_datasets` / `describe_dataset` | The catalog, and each dataset's schema with the exact dimension values |
| `query_sql` | Read-only DuckDB SQL for everything else |

There are also five prompts: `informe_inflacion`, `panorama_economico`, `evolucion_pobreza`, `comercio_exterior_pais` and `salario_real`. The resources are `indec://guia` (how to cite, period formats and INDEC's signs), `indec://datasets` and `indec://dataset/{table}`.

The server's instructions tell the model to cite "Fuente: INDEC" with the period of each figure, to flag provisional data, to prefer INDEC's published changes over computing its own, and to say when it has computed something.

### `query_sql` sandbox

- The database is opened read-only, through `ATTACH … (READ_ONLY)` on a fresh in-memory instance.
- `enable_external_access=false` means no file access, network access, `ATTACH` or extension installs.
- The configuration is locked, so a query can't turn those settings back on.
- Each call accepts one SELECT statement, runs for at most 5 s and returns at most 500 rows. At most 4 queries run at once.
- Tests cover `DROP`, `INSERT`, `COPY TO`, `read_csv('/etc/passwd')`, URLs and runaway joins.

## Run it locally

**As a user**, no clone needed: `uvx indec-mcp` (or `pipx run indec-mcp`, or `pip install indec-mcp`). On first start it downloads the daily snapshot of the mirror from the hosted server (about 12 MB) to your user data dir, then checks for a new one every 6 hours. No INDEC token needed. Environment variables:

- `INDEC_DB`: path to your own mirror file. If set, the server never downloads or replaces it.
- `INDEC_SNAPSHOT_URL`: where to download the snapshot from. An empty value turns downloads off.

**For development:**

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
echo 'INDEC_TOKEN=...' > .env            # git ignores this file; see "INDEC token" below

.venv/bin/indec-sync                     # full refresh -> data/indec.duckdb (~15 min)
.venv/bin/indec-sync --only ipc_nacional # refresh one dataset; the others are carried over
.venv/bin/python -m pytest               # fake PostgREST server and fixture DB, no network
```

- **Claude Code:** `.mcp.json` registers the local server (`.venv/bin/indec-mcp`, over stdio). Start `claude` in this folder and approve `indec` when asked.
- **Other clients:** run `.venv/bin/indec-mcp`. In a checkout it uses `data/indec.duckdb` and keeps it fresh from the snapshot like an installed copy does. Set `INDEC_SNAPSHOT_URL=""` to keep a mirror you synced yourself untouched.
- **HTTP, as in production:** run `SYNC_ENABLED=0 .venv/bin/indec-mcp-http`. It serves `http://localhost:8000/mcp`.

### How the sync works

- **Full refresh every run.** INDEC revises provisional figures, so appending only new periods would keep outdated values.
- **Stable pagination.** Each table is ordered by its full primary key, so pages never skip or repeat rows.
- **No half-written files.** Each run builds `indec.duckdb.tmp`, then replaces the old file in one step. The server reopens it on its own.
- **Partial failures are survivable.** If one dataset fails, its previous table is kept and marked `stale`. An expired token aborts the run and leaves the mirror untouched.
- **Built-in validation.** Row counts are checked against the API's reported total, and duplicate primary keys are flagged.

```sql
-- duckdb data/indec.duckdb
SELECT table_name, row_count, latest_periodo, status FROM _datasets ORDER BY row_count DESC;
```

### INDEC token

Sign up at <https://api.indec.gob.ar>, verify with the emailed code, and generate the token from your profile. It's shown only once. The FAQ says it lasts 12 months, but the one we got expires after **6**.

## Deploy

The hosted server runs on Railway from the `Dockerfile`. Configuration, abuse protection, the production rate-limit verification and routine operations are in [deploy.md](deploy.md).

## Other docs and scripts

- [api-research.md](api-research.md): how the INDEC API works (auth, PostgREST, limits, data quirks, terms), with measurements
- [datasets.md](datasets.md): all 19 datasets with their columns and primary keys (generated from the spec)
- [ideas.md](ideas.md): project ideas and roadmap
- `scripts/fetch-spec.sh`: refresh `spec/indec-openapi.json` and regenerate `datasets.md` (no token needed)
- `INDEC_TOKEN=... scripts/probe.sh`: row counts, latency and rate-limit headers for each dataset
