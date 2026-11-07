# INDEC API research

Researched 2026-11-07, five days after launch. Everything under "Verified" was checked with live requests against `api.indec.gob.ar`. Everything under "Unknown" needs a token, or an answer from INDEC.

## What it is

On 2026-11-02 INDEC launched the **Catálogo API-INDEC**, its first official API ([IProUP](https://www.iproup.com/economia-digital/72980-indec-lanza-una-api-para-el-uso-de-datos-publicos-con-ia), [Examedia](https://examedia.com.ar/portada/post/88166045)). The press says it was built with support from the IDB (BID). The service is labelled **BETA**.

- Portal and signup: <https://api.indec.gob.ar> (a Next.js app; the docs are rendered with Scalar)
- Data API: `https://api.indec.gob.ar/v1/` (it is **PostgREST** behind nginx: see the `Proxy-Status: PostgREST` header and the `PGRST*` error codes)
- Docs: <https://api.indec.gob.ar/docs>
- Contact: `ces@indec.gob.ar`, (54-11) 5031-4632

## Verified

### Auth

- Every data endpoint needs `Authorization: Bearer <JWT>`. A request without one returns `401 {"code":"42501","message":"permission denied for schema api"}`.
- To get a token: sign up with email, name and organization, verify with an emailed OTP, then generate the token from your profile. **It is shown only once.** The FAQ says it expires after 12 months, but the real one expires after **6** (see below). Generating a new one immediately revokes the old one.
- The **OpenAPI 3.1 spec is public**: `GET https://api.indec.gob.ar/v1/` works without a token. A snapshot is in [`spec/indec-openapi.json`](../spec/indec-openapi.json), and `scripts/fetch-spec.sh` refreshes it.

### Datasets (19 endpoints, 7 INDEC directorates)

See [datasets.md](datasets.md) for the full generated list, with columns and primary keys. In summary:

| Area | Endpoints |
|---|---|
| Prices | `ipc_nacional` (CPI, by region down to subclass, since Dec 2016), `valorizacion_canastas_cba_cbt` (basic food and total baskets, GBA, since Apr 2016), `sipm_precios_mayoristas` (IPIM/IPIB/IPP wholesale), `icc_precios_construccion`, `redeterminacion_precios_contratos_obras_publicas`, `indice_salarios` |
| Activity | `estimador-mensual-de-actividad-economica-nivel-general` and `-sectores` (EMAE), `ipi_manufacturero`, `ipi_minero` |
| Living conditions | `pobreza_eph` (poverty and indigence, half-yearly), `mercado_trabajo_eph` (labour market rates, quarterly), `canasta-crianza` (cost of raising children) |
| Population | national projections 2022–2040, projections by province 2022–2040, estimates by department 2022–2035 (based on the 2022 Census) |
| Foreign trade | `comercio_exterior_exportacion_bienes` (by sector and destination, since 1986), `comercio_exterior_importacion_bienes`, `comercio_exterior_nomenclaturas` (code dictionary) |

### Query language (PostgREST)

- `select=col1,col2`, `order=col.desc`, `limit`, `offset`
- Filters: `?col=eq.x`, plus `neq gt gte lt lte like ilike in is` (for example `?region=eq.Nacional`, `?anio=gte.2024`, `?descripcion=ilike.*alimentos*`)
- **At most 1000 rows per response**, whatever `limit` you ask for. To paginate, use `limit` and `offset`, and **order by the full composite primary key**, or rows can repeat or go missing between pages.
- `Prefer: count=exact` puts the total in `Content-Range: 0-999/103545`, and the response status becomes `206` when you get only part of the result.
- Only `GET`, `HEAD` and `OPTIONS` are allowed. The API is read-only.
- CORS is `Access-Control-Allow-Origin: *`, so a browser can call it directly. Doing that would expose the token, though.
- Errors are PostgREST JSON: `{code, message, details, hint}`. A bad filter or column returns 400, a bad or missing token 401, and no permission 403.

### Data semantics that matter for an AI layer

- **Data is in long ("tidy") format.** For example, one `ipc_nacional` row is one (region × classifier level × code × period × indicator type) combination. `tipo_de_indicador` mixes index levels, monthly changes and year-on-year changes **in the same table**. A naive query mixes them together, so the MCP has to pick the indicator type explicitly.
- **Null-reason signs.** `observaciones` columns carry INDEC's conventional signs: `*` provisional, `**` preliminary, `e` estimated, `i` imputed, `u` below-standard quality, `-` absolute zero, `.` not recorded, `...` not yet available, `///` not applicable, `s` confidential. **An AI answer should say when a figure is provisional.**
- **Rounding caveat (from the docs).** Changes computed from the API can differ from the ones published in technical reports. An assistant should **return INDEC's published change rows** and not compute its own from index levels. A community project has already reported mismatches between the API and the website ([datina PR](https://github.com/germanargsmart/datina/pull/73)).
- **Required citation:** "Fuente: INDEC", plus the specific dataset used (FAQ).

### Inconsistencies to normalize

- Path style mixes snake_case (`ipc_nacional`) and kebab-case (`canasta-crianza`, `estimador-mensual-...`).
- Column case varies: `Anio`, `Mes` and `Periodo` in the trade tables and `canasta-crianza`, but `anio`, `mes` and `periodo` elsewhere. `codigo_CLaNAE_2004` and `codigo_ClaNAE_2010` differ too.
- Trade tables contain only codes (`pais_cod`, `rubro_cod`, …), so they need a join with `comercio_exterior_nomenclaturas` to be readable.
- `canasta-crianza` is in wide format (`tot_men1`, `tot_1a3`, …), unlike everything else.
- Primary keys are long (up to 13 columns), and stable paging needs all of them.

### Terms of use

- The [terms](https://api.indec.gob.ar/ayuda/terminos) frame the data as open data published "to facilitate access and reuse". **They do not forbid redistribution or proxying.**
- The [FAQ](https://api.indec.gob.ar/ayuda/faq) says *"existen límites de uso … consulte los términos de uso para conocer los límites específicos"*, **but the terms page lists no limits.** That gap needs an answer.

## Measured with a token (2026-11-07)

These come from `scripts/probe.sh` and manual queries, using a `Personal` account.

### Token

- The JWT payload has `role: "basic"`, which suggests account tiers.
- The token was issued 2026-11-07 and **expires 2027-05-06, which is 6 months, not the 12 the FAQ states.** Plan for rotation twice a year.
- Signup asks for `account_type` (Personal / Empresarial / Educativa / ONG / Gubernamental / Otro) and at least one `intended_use`. The options come from `GET /api/auth/lookups`. There is no captcha.

### Size and speed

| Dataset | Rows | Latest period |
|---|---:|---|
| `comercio_exterior_exportacion_bienes` | 779,536 | 202609 |
| `comercio_exterior_importacion_bienes` | 390,315 | 202609 |
| `redeterminacion_precios_contratos_obras_publicas` | 236,192 | 202609 |
| `sipm_precios_mayoristas` | 234,260 | 202609 |
| `ipc_nacional` | 234,198 | 202609 |
| `ipi_manufacturero` | 41,667 | 202609 |
| `proyecciones_poblacion_jurisdiccion_2022_2040` | 19,152 | 2040 |
| `ipi_minero` | 17,784 | 202609 |
| `estimaciones_poblacion_departamento_2022_2035` | 14,784 | 2035 |
| `icc_precios_construccion` | 14,017 | 202609 |
| `estimador-…-sectores` | 8,512 | 202608 |
| `proyecciones_poblacion_nacional_2022_2040` | 3,838 | 2040 |
| `indice_salarios` | 3,406 | 202608 |
| `valorizacion_canastas_cba_cbt` | 1,638 | 202609 |
| `estimador-…-nivel-general` | 1,632 | 202608 |
| `comercio_exterior_nomenclaturas` | 325 | — |
| `canasta-crianza` | 81 | 202609 |
| `mercado_trabajo_eph` | 41 | 20262T |
| `pobreza_eph` | 20 | 20261S |
| **Total** | **≈2.0 M** | |

- Copying everything is about **2,010 requests** of 1000 rows each. At 1 request/s that takes about 35 minutes, and incremental syncs (`periodo=gt.<last>`) are much smaller. The whole dataset fits easily in a DuckDB file.
- Latency is 70–110 ms per request. The exception is a request on exports with `Prefer: count=exact`, which takes 0.6 s because counting 780k rows is expensive, so avoid exact counts on the big tables.
- `limit=5000` still returns 1000 rows. The cap is enforced.

### Rate limits

- No `RateLimit-*` or `Retry-After` headers were returned.
- 30 back-to-back requests all returned `200`. Either the limit is higher than that, or it is enforced some other way. I didn't push harder on purpose. We still need to ask INDEC what the real limit is.

### Data shape (IPC example, period 202609)

- `periodo` is a string in three formats: `YYYYMM` for monthly data, `YYYY{1,2}S` for half-yearly (`20261S`), and `YYYY{1-4}T` for quarterly (`20262T`). `fecha` is a string in two population tables and an integer in the third.
- A single month of IPC is **more than 1000 rows** (regions × levels × 8 indicator types), so even "just this month" needs pagination or tight filters.
- `tipo_de_indicador` values: `Indice`, `v_m` (change from the previous month), `v_ia` (year on year), `v_acum` (year to date), `Incidencia_m`, `Incidencia_ia`, `Incidencia_acum`, and `Precio promedio`.
- `nivel` values: `Nivel General`, `División`, `Grupo`, `Clase`, `Categoría` (Núcleo/Regulados/Estacional/Bienes/Servicios), and `Producto`. Regions are `Nacional`, `GBA`, `Pampeana`, `Noreste`, `Noroeste`, `Cuyo` and `Patagonia`.
- **Filters are case-sensitive.** The headline needs `nivel=eq.Nivel General` (capital G), while its `descripcion` is "Nivel general". An LLM writing raw filters will get this wrong.
- There is a `valor_null` column next to `valor`, which carries the null-reason sign, alongside `observaciones`.
- For the record, September 2026 national CPI was +1.9% m/m, +33.4% y/y and +23.6% year to date, with core inflation at +1.7% m/m.

## Still unknown

- [ ] The real rate limit, and whether `basic` accounts have a lower one than other tiers.
- [ ] Update timing: does the API update at the same moment INDEC publishes each report?
- [ ] Whether it is OK to serve data fetched with one personal token to many end users (a public MCP or mirror). **Email `ces@indec.gob.ar` before launching anything public.**

## Existing community work (as of 2026-11-07)

- [SantinoSpingola/indec-mcp](https://github.com/SantinoSpingola/indec-mcp): a TypeScript MCP that runs locally, where each user brings their own token. 0 stars, created this week.
- [julpar/indec-mcp](https://github.com/julpar/indec-mcp): has no description yet, and I haven't reviewed it.
- Projects that use the API for data, not AI: [datosargentina](https://github.com/francomartinlopez/datosargentina/pull/32), [macro-ar](https://github.com/santiagoalejandromurcia-hub/macro-ar/pull/20), [datina](https://github.com/germanargsmart/datina/pull/73).
- An [Apify scraper](https://apify.com/tuneup_argentina/argentina-indec/api), which is third-party and unofficial.

**Gap:** nothing **hosted** exists yet. Every current MCP needs each user to register with INDEC and paste a JWT, which is a lot of friction for non-developers who "just want to chat with INDEC data".
