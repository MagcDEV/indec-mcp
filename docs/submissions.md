# Directory submissions kit

Everything needed to list the hosted server. Prepared 2026-11-07, from each directory's own current docs.

| | |
|---|---|
| Endpoint | `https://indec-mcp-production.up.railway.app/mcp` (Streamable HTTP, no auth) |
| Repo / docs | https://github.com/MagcDEV/indec-mcp |
| Privacy policy | https://github.com/MagcDEV/indec-mcp/blob/main/PRIVACY.md |
| Support | https://github.com/MagcDEV/indec-mcp/issues |
| Icon | `docs/img/icon.png` (512×512, transparent) · raw: https://raw.githubusercontent.com/MagcDEV/indec-mcp/main/docs/img/icon.png |
| License | MIT |

## Order and status

| # | Directory | How | Who | Status |
|---|---|---|---|---|
| 1 | **Official MCP Registry** | `mcp-publisher` + `server.json` | You approve the GitHub login, then the CLI publishes | Ready (`server.json` validates against schema 2025-12-11) |
| 2 | **Anthropic Connectors Directory** | Portal at [claude.ai/directory/manage](https://claude.ai/directory/manage) | You (needs a paid Claude plan and 7 policy acknowledgments) | Ready: answers below. Every tool has a title and a read-only annotation |
| 3 | **Glama** | Picks up the official registry. `glama.json` claims maintainership | Automatic after #1 | Ready |
| 4 | **awesome-mcp-servers** (punkpeye) | Pull request | PR from your GitHub account | Entry ready |
| 5 | **awesome-remote-mcp-servers** (punkpeye) | Pull request | PR from your GitHub account | Entry ready |
| 6 | **MCP Market** | Web form with the repo URL | You | Ready |
| — | PulseMCP | Not accepting submissions right now. It says it will import from the official registry | — | Covered by #1 |
| — | Smithery | Its hosted proxy shut down on 2026-10-31, and it's now a plain directory run by Arcade.dev | — | Optional, later |

---

## 1. Official MCP Registry

The registry checks that we own the `io.github.MagcDEV` namespace through a GitHub login. It doesn't check the remote URL, as its docs state.

```bash
# install the publisher (official release, Linux x86-64)
curl -fsSL https://github.com/modelcontextprotocol/registry/releases/download/v1.8.8/mcp-publisher_linux_amd64.tar.gz | tar xz mcp-publisher
./mcp-publisher login github     # prints a code: open https://github.com/login/device and enter it
./mcp-publisher publish          # reads ./server.json
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.MagcDEV/indec-mcp"   # verify
```

To publish a new version later, bump `version` in `server.json` and `pyproject.toml`, then run `publish` again.

## 2. Anthropic Connectors Directory

Portal: [claude.ai/directory/manage](https://claude.ai/directory/manage) → **Submit new** → **MCP connector**. Requirements: [submission docs](https://claude.com/docs/connectors/building/submission). Escalations: `mcp-review@anthropic.com`.

| Portal step | Answer |
|---|---|
| **1. Connection** | `https://indec-mcp-production.up.railway.app/mcp`, or pick the INDEC custom connector you already added |
| **2. Tools** | The 12 tools sync automatically, all read-only and all titled. There are also 5 prompts and 3 resources |
| **3. Listing: name** (≤100) | `INDEC Argentina (no oficial)`. Don't put "MCP" or "Server" in the name, and keep "no oficial" so it's not mistaken for INDEC itself |
| **3. Listing: one-liner** (≤200) | `Argentina's official statistics from INDEC — inflation, wages, poverty, jobs, trade and population — with sources and periods cited. Unofficial; no signup.` |
| **3. Listing: categories** | Data & Analytics · Finance · Research (choose whatever the portal offers that's closest) |
| **3. Listing: docs / privacy / support** | Repo URL · PRIVACY.md URL · Issues URL (table above) |
| **3. Listing: icon** | `docs/img/icon.png` |
| **3. Listing: slug** | `indec-argentina` |
| **4. Use cases** | See "Use cases" below. Prerequisites: none. Access: **reads data only** |
| **5. Company** | Individual developer: MagcDEV, https://github.com/MagcDEV, plus your contact email |
| **6. Authentication** | **No authentication** (public data) |
| **7. Data handling** | Underlying API: **a third party's that you don't control**. It's INDEC's public open-data API (Catálogo API-INDEC), mirrored daily under INDEC's terms of use. No personal health data and no sponsored content |
| **8. Test & launch** | "No account needed. Add the URL as a custom connector, then ask: '¿Cuánto fue la inflación del último mes?', '¿Los salarios le ganaron a la inflación desde 2024?', '¿Cuánto exportó Argentina a China en 2025?', '¿Cuánta gente vive en Córdoba?'. Every tool is read-only." Confirm you ran every tool |
| **9. Compliance** | 7 acknowledgments, which you read and confirm yourself |

**Description (≤2000), English:**

> Ask about Argentina's economy and get answers built on official INDEC data (Instituto Nacional de Estadística y Censos), with the source, the period of each figure and provisional data clearly marked.
>
> Covers all 19 datasets of INDEC's new public API (Catálogo API-INDEC):
> - consumer prices by region and category since 2016
> - basic food and total baskets (the poverty line)
> - wages by sector
> - economic activity (EMAE) and industrial production
> - poverty and labour-market rates (EPH)
> - wholesale and construction prices
> - exports and imports since 1986 by country and product
> - population projections to 2040 by province and department
>
> Tools: headline indicators; CPI, wholesale-price, construction-cost and industrial-production series; side-by-side comparisons, such as real wages or basket vs. salary; foreign trade with plain-name lookup ("China", "MOA"); population; and a sandboxed read-only SQL tool for anything else.
>
> Built for correct answers. It uses INDEC's published percentage changes rather than recomputing them, labels any computed figure (such as real wages) as computed, and reports the rows withheld for statistical secrecy, so a partial sum is never presented as a total.
>
> No signup, no API token. Data is refreshed daily from INDEC. Unofficial project, not affiliated with INDEC. Open source (MIT).

**Descripción, en español** (por si el portal la acepta):

> Preguntá por la economía argentina y obtené respuestas con datos oficiales del INDEC, citando la fuente y el período de cada cifra y señalando los datos provisorios.
>
> Incluye los 19 datasets del Catálogo API-INDEC:
> - inflación (IPC) por región y categoría
> - canastas básicas
> - salarios
> - actividad económica (EMAE) e industria
> - pobreza y mercado de trabajo (EPH)
> - precios mayoristas y de la construcción
> - comercio exterior desde 1986
> - proyecciones de población hasta 2040
>
> Usa las variaciones que publica el INDEC, aclara cuando algo es un cálculo propio (por ejemplo, el salario real) e informa los datos confidenciales por secreto estadístico. Sin registro ni token. Se actualiza todos los días. Proyecto no oficial, no afiliado al INDEC. Código abierto (MIT).

**Use cases:**
1. Inflation briefings: the latest CPI, core inflation, which divisions rose most, regional differences.
2. Real wages: whether wages beat inflation since a given date, by sector.
3. Cost of living: the basic basket for a typical household vs. wages.
4. Trade analysis: exports and imports with a country, by year and product.
5. Demographics: population of a province or department, by sex or age, through 2040.

## 3. Glama

Glama ingests the official registry, so this follows from #1. `glama.json` in the repo root lists `MagcDEV` as maintainer, which lets you claim the listing at glama.ai with your GitHub login.

## 4. awesome-mcp-servers: PR

Section **📊 Data Platforms**, next to `bruno-portfolio/agrobr-mcp`. (Brazil's `ibge-br-mcp` is listed under Databases.) Keep the list's alphabetical order. Legend: 🐍 Python · ☁️ cloud service · 🏠 local service · 🍎🪟🐧 macOS/Windows/Linux.

```markdown
- [MagcDEV/indec-mcp](https://github.com/MagcDEV/indec-mcp) 🐍 ☁️ 🏠 🍎 🪟 🐧 - Argentina's official statistics (INDEC): inflation, wages, poverty, jobs, trade and population, from a daily DuckDB mirror of INDEC's API. Hosted endpoint with no signup, or run it locally.
```

PR title: `Add MagcDEV/indec-mcp (Argentina INDEC statistics)`. If the PR is opened by an AI agent, the list's CONTRIBUTING asks that the title end in `🤖🤖🤖`.

## 5. awesome-remote-mcp-servers: PR

Section **🔬 Science & Research**, near other official-statistics servers. Keep the alphabetical order. Entries are only accepted once the endpoint answers an MCP `initialize`, which ours does.

```markdown
- [INDEC Argentina (unofficial)](https://github.com/MagcDEV/indec-mcp) `https://indec-mcp-production.up.railway.app/mcp`
  🔓 - Argentina's official INDEC statistics: inflation, wages, poverty, jobs, trade and population, with sources cited.
```

## 6. MCP Market

Submit the repo URL `https://github.com/MagcDEV/indec-mcp` through their web form.
