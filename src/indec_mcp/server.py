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
from mcp_types import ToolAnnotations
from pydantic import Field

from . import topics
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
6. Para cualquier otra cosa: `list_datasets` → `describe_dataset` (muestra columnas y los valores \
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

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


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
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
    @cached
    def latest_indicators() -> dict[str, Any]:
        """Últimas cifras de titular del INDEC, cada una con su período: inflación (IPC nacional,
        general y núcleo), actividad (EMAE), salarios, canastas básicas CBA/CBT, pobreza e
        indigencia, desocupación y empleo. Usala primero para preguntas generales."""
        return mirror.snapshot()

    @server.tool(annotations=READ_ONLY)
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

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
    @cached
    def compare_series(
        series: Annotated[
            list[str],
            Field(description="2 a 4 ids de serie mensual: 'ipc[:categoría][@región]' (ej. 'ipc', "
                  "'ipc:Núcleo', 'ipc:Alimentos@GBA'), 'salarios[:sector]' (sector: registrado, no registrado, "
                  "privado registrado, público), 'cbt' / 'cba' (canastas básicas GBA, en pesos), 'emae'."),
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
        2 series, la variación relativa (ej. salario real). El resumen es CALCULADO: decilo al usarlo."""
        return mirror.compare(series, measure, start, end)

    @server.tool(annotations=READ_ONLY)
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
        (secreto estadístico) y el resultado informa cuántas son: mencionalo al citar montos."""
        return topics.foreign_trade(mirror, flow, country, category, start, end, group_by, top)

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
    @cached
    def trade_codes(
        query: Annotated[str, Field(description="Nombre o código a buscar, ej. 'china', 'carnes', 'MOI'.")],
    ) -> list[dict[str, Any]]:
        """Busca códigos del diccionario de comercio exterior (países, rubros, subrubros, usos económicos
        y grandes categorías económicas). Ignora mayúsculas y acentos."""
        return topics.trade_codes(mirror, query)

    @server.tool(annotations=READ_ONLY)
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

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
    @cached
    def list_datasets() -> list[dict[str, Any]]:
        """Los datasets del mirror (nombre de tabla SQL, descripción, filas, último período, estado
        de sincronización)."""
        return mirror.datasets()

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
    @cached
    def describe_dataset(
        table: Annotated[str, Field(description="Nombre de tabla de `list_datasets`, ej. 'ipc_nacional'.")],
    ) -> dict[str, Any]:
        """Esquema de un dataset: descripción, clave primaria, columnas con tipo y significado, los
        valores exactos de cada dimensión de texto (usalos tal cual en filtros SQL: distinguen
        mayúsculas) y filas de ejemplo. Llamala antes de escribir `query_sql`."""
        return mirror.describe(table)

    query_doc = (
        "Consulta SQL de solo lectura (DuckDB) sobre las tablas del mirror. Límites: una sola "
        f"sentencia SELECT, máx. {mirror.limits.max_rows} filas, {mirror.limits.timeout_s:g}s. Sin acceso a "
        "archivos ni red. Agregá y filtrá en SQL en lugar de traer filas crudas; los períodos son "
        "texto ('202609')."
    )

    @server.tool(annotations=READ_ONLY, description=query_doc)
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
