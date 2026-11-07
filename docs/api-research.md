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
- To get a token: sign up with email, name and organization, verify with an emailed OTP, then generate the token from your profile. **It is shown only once.** It **expires after 12 months**. Generating a new one immediately revokes the old one.
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

## Unknown (needs a token, or a question to INDEC)

- [ ] Actual rate limits, and whether the API sends `RateLimit`/`Retry-After` headers. Run `INDEC_TOKEN=… scripts/probe.sh`.
- [ ] Row count per dataset. Exports since 1986 by country and sub-sector is probably the big one. `probe.sh` reports this through `Content-Range`.
- [ ] Update cadence and timing: does the API update on the release day of each INDEC report?
- [ ] Is it allowed to serve data fetched with one personal token to many end users (a public MCP or mirror)? **Email `ces@indec.gob.ar` before launching anything public.**
- [ ] Whether there are account types or tiers (the FAQ mentions "su tipo de cuenta").

## Existing community work (as of 2026-11-07)

- [SantinoSpingola/indec-mcp](https://github.com/SantinoSpingola/indec-mcp): a TypeScript MCP that runs locally, where each user brings their own token. 0 stars, created this week.
- [julpar/indec-mcp](https://github.com/julpar/indec-mcp): has no description yet, and I haven't reviewed it.
- Projects that use the API for data, not AI: [datosargentina](https://github.com/francomartinlopez/datosargentina/pull/32), [macro-ar](https://github.com/santiagoalejandromurcia-hub/macro-ar/pull/20), [datina](https://github.com/germanargsmart/datina/pull/73).
- An [Apify scraper](https://apify.com/tuneup_argentina/argentina-indec/api), which is third-party and unofficial.

**Gap:** nothing **hosted** exists yet. Every current MCP needs each user to register with INDEC and paste a JWT, which is a lot of friction for non-developers who "just want to chat with INDEC data".
