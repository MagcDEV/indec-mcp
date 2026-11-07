"""MCP server exposing the INDEC mirror. Run locally over stdio: `indec-mcp`.

Tool design: a few intent-level tools that hide the API's traps (mixed indicator types in one
table, case-sensitive category names, region-only detail levels), plus `describe_dataset` and a
sandboxed `query_sql` escape hatch for everything else.
"""

from __future__ import annotations

import os
import re
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import Icon, ToolAnnotations
from pydantic import Field

from . import indices, living, topics
from .mirror import IPC_MEASURES, IPC_REGIONS, SOURCE, Limits, Mirror, QueryError

INSTRUCTIONS = """\
Datos oficiales del INDEC (Argentina) desde un mirror local del Catálogo API-INDEC: inflación \
(IPC), canastas básicas (CBA/CBT), salarios, actividad (EMAE), industria (IPI), pobreza y mercado \
de trabajo (EPH), precios mayoristas y de la construcción, comercio exterior y población.

Cómo usar estas herramientas:
1. Para "¿cómo está la economía?" o cifras de titular, empezá por `latest_indicators`.
2. Para inflación (general, núcleo, por división o región) usá `inflation_series`.
3. Para comparar series (salario real, salarios vs canasta, regiones, actividad vs precios) usá \
`compare_series`.
4. Comercio exterior (exportaciones/importaciones por país, rubro, mes o año): `foreign_trade`. \
Acepta nombres ("Brasil", "MOA", "bienes de capital"); `trade_codes` busca códigos.
5. Población (país, provincia o departamento; por sexo o edad; 2022-2040): `population`.
6. Precios mayoristas: `wholesale_prices`. Costo de la construcción: `construction_costs`. Producción \
industrial y minera: `industrial_production`. Actividad (EMAE, general o por sector): `economic_activity`.
7. Salarios: `wages`. Canastas básicas y de crianza: `basic_baskets`. Pobreza: `poverty`. Empleo y \
desocupación: `labor_market`.
8. Para cualquier otra cosa: `list_datasets` → `describe_dataset` (muestra columnas y los valores \
exactos de cada dimensión; los filtros distinguen mayúsculas) → `query_sql`.

Reglas al responder:
- Citá siempre "Fuente: INDEC" y el dataset usado, con el período de cada cifra.
- Si `observaciones` dice "Dato provisorio" (u otro signo), aclaralo.
- Usá las variaciones publicadas (v_m, v_ia, v_acum) en lugar de calcularlas desde el índice: \
INDEC advierte diferencias por redondeo. Si calculás algo (ej. salario real), decilo explícitamente.
- En comercio exterior, las sumas excluyen filas confidenciales (secreto estadístico): decilo.
- Períodos: 'AAAAMM' mensual, 'AAAA1S' semestral, 'AAAA1T' trimestral.
- Este servicio no es oficial ni está afiliado al INDEC.
"""

VERSION = "0.4.3"
REPO_URL = "https://github.com/MagcDEV/indec-mcp"
ICON_URL = "https://raw.githubusercontent.com/MagcDEV/indec-mcp/main/docs/img/icon.png"

# Shared parameter types (module level: annotations are evaluated against module globals).
StartPeriod = Annotated[str | None, Field(description="Desde, 'AAAA-MM' o 'AAAA'.")]
EndPeriod = Annotated[str | None, Field(description="Hasta, 'AAAA-MM' o 'AAAA' (inclusive).")]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def read_only(title: str) -> ToolAnnotations:
    """Read-only annotations carrying the human-readable title.

    Anthropic's Connectors Directory reads the title from `annotations.title` (not only the
    tool-level `title`), so every tool sets both.
    """
    return READ_ONLY.model_copy(update={"title": title})


def _tool_errors(fn):
    """Turn expected data errors into MCP tool errors the model can read and recover from."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except QueryError as e:
            raise ToolError(str(e)) from None

    return wrapper


def _whitespace_normalized(kw: dict) -> dict:
    if isinstance(kw.get("sql"), str):
        kw = {**kw, "sql": re.sub(r"[\s;]+$", "", " ".join(kw["sql"].split()))}
    return kw


def create_server(mirror: Mirror) -> MCPServer:
    def cached(fn):
        """Serve repeated calls from the per-mirror-version cache (data changes once a day)."""

        @wraps(fn)
        def wrapper(**kwargs):
            return mirror.cached(fn.__name__, _whitespace_normalized(kwargs), lambda: fn(**kwargs))

        return wrapper

    server = MCPServer(
        name="indec",
        title="INDEC Argentina (no oficial)",
        description="Estadísticas oficiales del INDEC (Argentina): inflación, salarios, pobreza, comercio, población.",
        instructions=INSTRUCTIONS,
        website_url=REPO_URL,
        icons=[Icon(src=ICON_URL, mime_type="image/png", sizes=["512x512"])],
        version=VERSION,
    )

    @server.tool(title="Últimos indicadores del INDEC", annotations=read_only("Últimos indicadores del INDEC"))
    @_tool_errors
    @cached
    def latest_indicators() -> dict[str, Any]:
        """Últimas cifras de titular del INDEC, cada una con su período: inflación (IPC nacional,
        general y núcleo), actividad (EMAE), salarios, canastas básicas CBA/CBT, pobreza e
        indigencia, desocupación y empleo."""
        return mirror.snapshot()

    @server.tool(title="Serie de inflación (IPC)", annotations=read_only("Serie de inflación (IPC)"))
    @_tool_errors
    @cached
    def inflation_series(
        category: Annotated[
            str,
            Field(description="Nombre o código: 'Nivel general', 'Núcleo', 'Regulados', 'Estacional', "
                  "'Bienes', 'Servicios', o una división como 'Alimentos', 'Transporte', 'Vivienda'. "
                  "Grupos y clases solo existen por región, no para Nacional."),
        ] = "Nivel general",
        region: Annotated[str, Field(description=f"Una de: {', '.join(IPC_REGIONS)}")] = "Nacional",
        measures: Annotated[
            list[str] | None,
            Field(description="Medidas: " + "; ".join(f"{k} = {v}" for k, v in IPC_MEASURES.items())
                  + ". Por defecto ['v_m', 'v_ia']."),
        ] = None,
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM'. Por defecto, últimos 13 meses.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA-MM' (inclusive).")] = None,
    ) -> dict[str, Any]:
        """Serie mensual del Índice de Precios al Consumidor (IPC, base dic-2016) desde diciembre
        2016: variación mensual, interanual, acumulada, índice o incidencias, por categoría y región."""
        return mirror.ipc_series(category, region, measures, start, end)

    @server.tool(title="Comparar series (salario real, canasta, regiones)", annotations=read_only("Comparar series (salario real, canasta, regiones)"))
    @_tool_errors
    @cached
    def compare_series(
        series: Annotated[
            list[str],
            Field(description="2 a 4 ids de serie mensual: 'ipc[:categoría][@región]' (ej. 'ipc', "
                  "'ipc:Núcleo', 'ipc:Alimentos@GBA'), 'salarios[:sector]' (sector: registrado, no registrado, "
                  "privado registrado, público), 'cbt' / 'cba' (canastas básicas GBA, en pesos), 'emae', "
                  "'ipim' / 'ipib' / 'ipp' (mayoristas), 'icc[:categoría]' (construcción, ej. 'icc:Mano de obra'), "
                  "'ipi[:rama]' (industria), 'ipi_minero' y 'emae:sector' (ej. 'emae:Construcción')."),
        ],
        measure: Annotated[
            str,
            Field(description="'v_ia' variación interanual publicada (por defecto), 'v_m' mensual publicada, "
                  "o 'base100' niveles rebasados a 100 en el primer mes."),
        ] = "v_ia",
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM'. Por defecto, últimos 13 meses en común.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA-MM' (inclusive).")] = None,
    ) -> dict[str, Any]:
        """Compara series mensuales en los meses que tienen en común: salarios vs inflación (salario
        real), canasta básica vs salarios, IPC por región o categoría, actividad vs precios. Devuelve
        la tabla y un resumen con la variación entre el primer y el último mes de cada serie y, con
        2 series, la variación relativa (ej. salario real). El resumen es un cálculo del servicio, no una cifra
        oficial del INDEC."""
        return mirror.compare(series, measure, start, end)

    @server.tool(title="Comercio exterior (exportaciones e importaciones)", annotations=read_only("Comercio exterior (exportaciones e importaciones)"))
    @_tool_errors
    @cached
    def foreign_trade(
        flow: Annotated[str, Field(description="'exportaciones' (valor FOB) o 'importaciones' (valor CIF).")] = "exportaciones",
        country: Annotated[str | None, Field(description="País por nombre, ej. 'Brasil', 'China', 'Estados Unidos'.")] = None,
        category: Annotated[
            str | None,
            Field(description="Exportaciones: rubro ('Productos primarios', 'MOA', 'MOI', 'Combustible y energía') "
                  "o subrubro. Importaciones: uso económico ('Bienes de capital', 'Bienes intermedios', ...) o "
                  "gran categoría económica."),
        ] = None,
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM' o 'AAAA'. Por defecto, últimos 12 meses.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA-MM' o 'AAAA' (inclusive).")] = None,
        group_by: Annotated[
            str, Field(description="'mes' (por defecto), 'anio', 'pais', 'categoria' o 'subcategoria'.")
        ] = "mes",
        top: Annotated[int, Field(description="Máximo de filas al agrupar por país o categoría (1-50).")] = 15,
    ) -> dict[str, Any]:
        """Comercio exterior de bienes desde 1986: millones de USD y toneladas, agrupado por mes, año,
        país o categoría, con nombres en lugar de códigos. Las sumas excluyen las filas confidenciales
        (secreto estadístico); el resultado informa cuántas son."""
        return topics.foreign_trade(mirror, flow, country, category, start, end, group_by, top)

    @server.tool(title="Buscar códigos de comercio exterior", annotations=read_only("Buscar códigos de comercio exterior"))
    @_tool_errors
    @cached
    def trade_codes(
        query: Annotated[str, Field(description="Nombre o código a buscar, ej. 'china', 'carnes', 'MOI'.")],
    ) -> list[dict[str, Any]]:
        """Busca códigos del diccionario de comercio exterior (países, rubros, subrubros, usos económicos
        y grandes categorías económicas). Ignora mayúsculas y acentos."""
        return topics.trade_codes(mirror, query)

    @server.tool(title="Población (país, provincia, departamento)", annotations=read_only("Población (país, provincia, departamento)"))
    @_tool_errors
    @cached
    def population(
        place: Annotated[
            str | None,
            Field(description="Vacío o 'Argentina' para el país; una provincia ('Córdoba', 'CABA'); o un "
                  "departamento ('Rosario'; si es ambiguo, 'San Martín, Mendoza')."),
        ] = None,
        year: Annotated[int | None, Field(description="Año (2022-2040; departamentos hasta 2035). Por defecto, el actual.")] = None,
        by: Annotated[
            str, Field(description="'sexo' (por defecto), 'edad' (grupos de 5 años; país y provincias) o "
                       "'departamento' (los departamentos de una provincia).")
        ] = "sexo",
    ) -> dict[str, Any]:
        """Población estimada y proyectada al 1° de julio (base Censo 2022) para el país, una provincia
        o un departamento, con apertura por sexo, grupos de edad o departamentos."""
        return topics.population(mirror, place, year, by)

    @server.tool(title="Salarios (índice de salarios)", annotations=read_only("Salarios (índice de salarios)"))
    @_tool_errors
    @cached
    def wages(
        sector: Annotated[
            str,
            Field(description="'nivel general' (por defecto), 'registrado', 'no registrado', 'privado registrado', "
                  "'público', 'público nacional' o 'público provincial'."),
        ] = "nivel general",
        measures: Annotated[
            list[str] | None,
            Field(description="'v_m' mensual, 'v_ia' interanual, 'v_acum' acumulada en el año, 'indice' "
                  "(base oct-2016 = 100). Por defecto ['v_m', 'v_ia']."),
        ] = None,
        start: StartPeriod = None,
        end: EndPeriod = None,
    ) -> dict[str, Any]:
        """Evolución de los salarios por sector (índice de salarios, desde oct-2015): variaciones mensual,
        interanual y acumulada publicadas por el INDEC."""
        code = mirror._resolve_wage_code(sector or "nivel general")
        return indices.series(mirror, "SALARIOS", code, measures, start, end)

    @server.tool(title="Actividad económica (EMAE)", annotations=read_only("Actividad económica (EMAE)"))
    @_tool_errors
    @cached
    def economic_activity(
        sector: Annotated[
            str | None,
            Field(description="Vacío = nivel general. Sector, ej. 'Construcción', 'Industria manufacturera', "
                  "'Comercio', 'Agricultura', 'Intermediación financiera', 'Explotación de minas y canteras'."),
        ] = None,
        measures: Annotated[
            list[str] | None,
            Field(description="Nivel general: 'v_ia' (original), 'v_m' (desestacionalizada), 'indice', "
                  "'indice_desestacionalizado', 'v_m_tendencia', 'indice_tendencia'. Sectores: 'v_ia', 'indice'. "
                  "Por defecto ['v_ia', 'v_m'] (general) o ['v_ia'] (sector)."),
        ] = None,
        start: StartPeriod = None,
        end: EndPeriod = None,
    ) -> dict[str, Any]:
        """Estimador mensual de actividad económica (EMAE, base 2004 = 100): nivel general desde 2004 y 16
        sectores. Es el indicador mensual que anticipa el PIB."""
        general = indices._norm(sector or "") in indices._GENERAL
        return indices.series(mirror, "EMAE_GENERAL" if general else "EMAE_SECTORES",
                              None if general else sector, measures, start, end)

    @server.tool(title="Canastas básicas (CBA, CBT) y canasta de crianza", annotations=read_only("Canastas básicas (CBA, CBT) y canasta de crianza"))
    @_tool_errors
    @cached
    def basic_baskets(
        basket: Annotated[
            str,
            Field(description="'CBT' canasta básica total = línea de pobreza (por defecto), 'CBA' alimentaria = "
                  "línea de indigencia, o 'crianza' (costo de criar a un niño o niña)."),
        ] = "CBT",
        age_group: Annotated[
            str | None,
            Field(description="Solo para 'crianza': 'menores de 1', '1 a 3', '4 a 5' o '6 a 12' (años). Vacío = "
                  "total de cada tramo."),
        ] = None,
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM'. Por defecto, últimos 13 meses.")] = None,
        end: EndPeriod = None,
    ) -> dict[str, Any]:
        """Valor en pesos de las canastas básicas del Gran Buenos Aires (desde abr-2016) por adulto equivalente y
        para los 3 hogares tipo, con sus variaciones; o la canasta de crianza (desde 2020) por tramo de edad."""
        return living.basic_baskets(mirror, basket, age_group, start, end)

    @server.tool(title="Pobreza e indigencia (EPH)", annotations=read_only("Pobreza e indigencia (EPH)"))
    @_tool_errors
    @cached
    def poverty(
        start: Annotated[str | None, Field(description="Desde, 'AAAA' o semestre 'AAAA1S'/'AAAA2S'. Vacío = toda la serie.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA' o 'AAAA1S'/'AAAA2S'.")] = None,
    ) -> dict[str, Any]:
        """Porcentaje de personas y hogares bajo la línea de pobreza e indigencia, semestral desde el 2° semestre
        de 2016 (total 31 aglomerados urbanos, EPH)."""
        return living.poverty(mirror, start, end)

    @server.tool(title="Mercado de trabajo (EPH)", annotations=read_only("Mercado de trabajo (EPH)"))
    @_tool_errors
    @cached
    def labor_market(
        start: Annotated[str | None, Field(description="Desde, 'AAAA' o trimestre 'AAAA1T'…'AAAA4T'. Vacío = toda la serie.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA' o 'AAAA1T'…'AAAA4T'.")] = None,
    ) -> dict[str, Any]:
        """Tasas trimestrales de actividad, empleo, desocupación, empleo formal e informal y subocupación desde
        el 2° trimestre de 2016 (total 31 aglomerados urbanos, EPH)."""
        return living.labor_market(mirror, start, end)

    @server.tool(title="Precios mayoristas (IPIM, IPIB, IPP)", annotations=read_only("Precios mayoristas (IPIM, IPIB, IPP)"))
    @_tool_errors
    @cached
    def wholesale_prices(
        index: Annotated[
            str,
            Field(description="'IPIM' (internos al por mayor, nacionales e importados, con impuestos; por defecto), "
                  "'IPIB' (básicos, sin impuestos) o 'IPP' (del productor, solo nacionales)."),
        ] = "IPIM",
        category: Annotated[
            str | None,
            Field(description="Vacío = nivel general. Origen: 'Productos nacionales' (N) / importados (I). Sector "
                  "o rubro CIIU, ej. 'Productos agropecuarios', 'Productos manufacturados', 'Energía eléctrica', "
                  "'Cereales y oleaginosas'. También acepta el código de la categoría."),
        ] = None,
        measures: Annotated[
            list[str] | None,
            Field(description="'v_m' mensual, 'v_ia' interanual, 'v_acum' acumulada, 'indice', 'incidencia'. "
                  "Por defecto ['v_m', 'v_ia']."),
        ] = None,
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM'. Por defecto, últimos 13 meses.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA-MM' (inclusive).")] = None,
    ) -> dict[str, Any]:
        """Precios mayoristas (Sistema de Índices de Precios Mayoristas, desde dic-2015): IPIM, IPIB o IPP,
        por nivel general, origen (nacional/importado), sector o rubro. Variaciones publicadas por el INDEC."""
        key = (index or "IPIM").strip().upper()
        if key not in indices.WHOLESALE_INDICES:
            raise QueryError("index debe ser 'IPIM', 'IPIB' o 'IPP'.")
        return indices.series(mirror, key, category, measures, start, end)

    @server.tool(title="Costo de la construcción (ICC)", annotations=read_only("Costo de la construcción (ICC)"))
    @_tool_errors
    @cached
    def construction_costs(
        category: Annotated[
            str | None,
            Field(description="Vacío = nivel general. Capítulos: 'Materiales', 'Mano de obra', 'Gastos generales'. "
                  "Grupos o ítems de obra, ej. 'Albañilería', 'Instalación eléctrica', 'Pintura', 'Estructura'; "
                  "o un material."),
        ] = None,
        measures: Annotated[
            list[str] | None,
            Field(description="'v_m' mensual, 'v_ia' interanual, 'v_acum' acumulada, 'indice', 'incidencia'. "
                  "Por defecto ['v_m', 'v_ia']."),
        ] = None,
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM'. Por defecto, últimos 13 meses.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA-MM' (inclusive).")] = None,
    ) -> dict[str, Any]:
        """Índice del costo de la construcción (ICC) del Gran Buenos Aires, desde nov-2015: nivel general,
        materiales, mano de obra, gastos generales, ítems de obra y materiales por producto."""
        return indices.series(mirror, "ICC", category, measures, start, end)

    @server.tool(title="Producción industrial (IPI manufacturero y minero)", annotations=read_only("Producción industrial (IPI manufacturero y minero)"))
    @_tool_errors
    @cached
    def industrial_production(
        sector: Annotated[str, Field(description="'manufacturera' (por defecto) o 'minera'.")] = "manufacturera",
        category: Annotated[
            str | None,
            Field(description="Vacío = nivel general. Manufactura: división o rama, ej. 'Alimentos y bebidas', "
                  "'Vehículos automotores, carrocerías, remolques y autopartes', 'Industrias metálicas básicas', "
                  "'Vino'. Minería: ej. 'Petróleo crudo', 'Gas natural', 'Extracción de minerales metalíferos'."),
        ] = None,
        measures: Annotated[
            list[str] | None,
            Field(description="'v_ia' interanual y 'v_acum' acumulada (serie original); 'v_m' mensual "
                  "(desestacionalizada); 'indice', 'indice_desestacionalizado', 'v_m_tendencia', "
                  "'indice_tendencia'. Por defecto ['v_ia', 'v_m']."),
        ] = None,
        start: Annotated[str | None, Field(description="Desde, 'AAAA-MM'. Por defecto, últimos 13 meses.")] = None,
        end: Annotated[str | None, Field(description="Hasta, 'AAAA-MM' (inclusive).")] = None,
    ) -> dict[str, Any]:
        """Índice de producción industrial (IPI): manufacturero desde 2016 (por división y rama) y minero desde
        2017 (petróleo, gas, minerales, rocas). Variaciones publicadas por el INDEC."""
        key = {"manufacturera": "IPI_MANUFACTURERO", "manufacturero": "IPI_MANUFACTURERO", "industria":
               "IPI_MANUFACTURERO", "minera": "IPI_MINERO", "minero": "IPI_MINERO", "mineria": "IPI_MINERO",
               "minería": "IPI_MINERO"}.get((sector or "manufacturera").strip().lower())
        if key is None:
            raise QueryError("sector debe ser 'manufacturera' o 'minera'.")
        return indices.series(mirror, key, category, measures, start, end)

    @server.tool(title="Listar datasets", annotations=read_only("Listar datasets"))
    @_tool_errors
    @cached
    def list_datasets() -> list[dict[str, Any]]:
        """Los datasets del mirror (nombre de tabla SQL, descripción, filas, último período, estado
        de sincronización)."""
        return mirror.datasets()

    @server.tool(title="Describir un dataset", annotations=read_only("Describir un dataset"))
    @_tool_errors
    @cached
    def describe_dataset(
        table: Annotated[str, Field(description="Nombre de la tabla SQL del dataset, ej. 'ipc_nacional'.")],
    ) -> dict[str, Any]:
        """Esquema de un dataset: descripción, clave primaria, columnas con tipo y significado, los
        valores exactos de cada dimensión de texto (usalos tal cual en filtros SQL: distinguen
        mayúsculas) y filas de ejemplo."""
        return mirror.describe(table)

    query_doc = (
        "Consulta SQL de solo lectura (DuckDB) sobre las tablas del mirror. Límites: una sola "
        f"sentencia SELECT, máx. {mirror.limits.max_rows} filas, {mirror.limits.timeout_s:g}s. Sin acceso a "
        "archivos ni red. Los períodos son "
        "texto ('202609')."
    )

    @server.tool(title="Consulta SQL de solo lectura", annotations=read_only("Consulta SQL de solo lectura"), description=query_doc)
    @_tool_errors
    @cached
    def query_sql(
        sql: Annotated[str, Field(description="Una única consulta SELECT en dialecto DuckDB.")],
    ) -> dict[str, Any]:
        return mirror.query(sql)

    _register_prompts_and_resources(server, mirror)
    return server


GUIDE = """# Guía de uso — INDEC MCP (no oficial)

## Cómo citar
Siempre "Fuente: INDEC", el dataset y el período de cada cifra. Este servicio no es oficial.

## Períodos
- Mensual: `AAAAMM` (ej. `202609`). Semestral: `AAAA1S`/`AAAA2S`. Trimestral: `AAAA1T`…`AAAA4T`.
- Los datasets tienen distinta fecha de último dato (ej. salarios va un mes detrás del IPC).

## Tipos de indicador (IPC, salarios, canastas)
`Indice` nivel · `v_m` variación mensual · `v_ia` interanual · `v_acum` acumulada en el año ·
`Incidencia_*` aporte a la variación del nivel general. Preferí las variaciones publicadas a calcularlas.

## Signos convencionales del INDEC
Cada columna de valor tiene una gemela `<col>_null` que explica un valor nulo:

| Signo | Significado |
|---|---|
| `*` | Dato provisorio |
| `**` | Dato preliminar |
| `e` | Estimado por extrapolación o proyección |
| `i` | Estimado por imputación |
| `u` | Calidad inferior al estándar |
| `-` | Cero absoluto |
| `.` | Dato no registrado |
| `...` | No disponible a la fecha |
| `///` | No corresponde presentar |
| `s` | Confidencial (secreto estadístico) |

La columna `observaciones` puede decir "Dato provisorio": mencionalo.

## Comercio exterior
Cerca de la mitad de las filas de detalle son confidenciales (`s`). Las sumas de este servicio las
excluyen, así que pueden ser menores que los totales oficiales.
"""


def _register_prompts_and_resources(server: MCPServer, mirror: Mirror) -> None:
    @server.resource("indec://guia", name="guia", title="Guía de uso y signos convencionales",
                     description="Cómo citar, formatos de período, tipos de indicador y signos del INDEC.",
                     mime_type="text/markdown")
    def guide() -> str:
        return GUIDE

    @server.resource("indec://datasets", name="datasets", title="Catálogo de datasets",
                     description="Datasets del mirror con filas, último período y estado.",
                     mime_type="application/json")
    def catalog() -> list[dict]:
        try:
            return mirror.datasets()
        except QueryError as e:
            return [{"error": str(e)}]

    @server.resource("indec://dataset/{table}", name="dataset", title="Esquema de un dataset",
                     description="Columnas, valores de dimensiones y ejemplos de un dataset.",
                     mime_type="application/json")
    def dataset(table: str) -> dict:
        try:
            return mirror.describe(table)
        except QueryError as e:
            return {"error": str(e)}

    @server.prompt(title="Informe de inflación")
    def informe_inflacion() -> str:
        """Informe breve del último IPC: general, núcleo, divisiones que más subieron y regiones."""
        return (
            "Armá un informe breve de la inflación del último mes publicado por el INDEC: variación mensual, "
            "interanual y acumulada del nivel general y de la núcleo (`latest_indicators`); las divisiones que "
            "más subieron en el mes (`query_sql` sobre ipc_nacional, nivel 'División', región 'Nacional', "
            "tipo v_m); y la comparación entre regiones (`compare_series` con 'ipc@GBA', 'ipc@Noreste', etc.). "
            "Terminá con una tabla y la fuente con el período."
        )

    @server.prompt(title="Panorama económico")
    def panorama_economico() -> str:
        """Resumen de la economía con las últimas cifras de cada área."""
        return (
            "Hacé un panorama de la economía argentina con las últimas cifras del INDEC: inflación, actividad "
            "(EMAE), salarios, canasta básica, pobreza y desocupación (`latest_indicators`). Agregá si los "
            "salarios le ganan a la inflación en los últimos 12 meses (`compare_series` con ['salarios', 'ipc']). "
            "Indicá el período de cada cifra y cuáles son provisorias."
        )

    @server.prompt(title="Evolución de la pobreza")
    def evolucion_pobreza() -> str:
        """Serie semestral de pobreza e indigencia y su relación con la canasta básica."""
        return (
            "Mostrá la evolución semestral de la pobreza y la indigencia en personas y hogares desde 2016 "
            "(`query_sql` sobre pobreza_eph, ordenado por período), señalando los datos provisorios. Contextualizá "
            "con la variación interanual de la canasta básica total (`compare_series` con ['cbt', 'salarios'])."
        )

    @server.prompt(title="Comercio exterior con un país")
    def comercio_exterior_pais(pais: str) -> str:
        """Exportaciones e importaciones con un país, por año y por rubro."""
        return (
            f"Analizá el comercio exterior de Argentina con {pais}: exportaciones e importaciones por año en los "
            f"últimos 5 años (`foreign_trade` con country='{pais}', group_by='anio'), el saldo, y qué rubros se "
            f"exportan (group_by='categoria'). Aclarar que las sumas excluyen filas confidenciales."
        )

    @server.prompt(title="Salario real")
    def salario_real(desde: str = "2024-01") -> str:
        """Si los salarios le ganaron a la inflación desde una fecha, por sector."""
        return (
            f"¿Los salarios le ganaron a la inflación desde {desde}? Usá `compare_series` con measure='base100' "
            f"y start='{desde}' para ['salarios', 'ipc'], ['salarios:registrado', 'ipc'] y "
            f"['salarios:no registrado', 'ipc']. Mostrá el resultado relativo de cada uno, aclarando que es "
            f"un cálculo propio y el rezago de los salarios no registrados."
        )


def main() -> None:
    # Default to the repo's data/ dir so the server works regardless of the client's cwd.
    default = Path(__file__).resolve().parents[2] / "data" / "indec.duckdb"
    db = os.environ.get("INDEC_DB") or str(default)
    mirror = Mirror(db, Limits())
    create_server(mirror).run("stdio")


if __name__ == "__main__":
    main()
