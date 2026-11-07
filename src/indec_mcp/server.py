"""MCP server exposing the INDEC mirror. Run locally over stdio: `indec-mcp`.

Tool design: a few intent-level tools that hide the API's traps (mixed indicator types in one
table, case-sensitive category names, region-only detail levels), plus `describe_dataset` and a
sandboxed `query_sql` escape hatch for everything else.
"""

from __future__ import annotations

import os
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .mirror import IPC_MEASURES, IPC_REGIONS, Limits, Mirror, QueryError

INSTRUCTIONS = """\
Datos oficiales del INDEC (Argentina) desde un mirror local del Catálogo API-INDEC: inflación \
(IPC), canastas básicas (CBA/CBT), salarios, actividad (EMAE), industria (IPI), pobreza y mercado \
de trabajo (EPH), precios mayoristas y de la construcción, comercio exterior y población.

Cómo usar estas herramientas:
1. Para "¿cómo está la economía?" o cifras de titular, empezá por `latest_indicators`.
2. Para inflación (general, núcleo, por división o región) usá `inflation_series`.
3. Para comparar series (salario real, salarios vs canasta, regiones, actividad vs precios) usá \
`compare_series`.
4. Para cualquier otra cosa: `list_datasets` → `describe_dataset` (muestra columnas y los valores \
exactos de cada dimensión; los filtros distinguen mayúsculas) → `query_sql`.

Reglas al responder:
- Citá siempre "Fuente: INDEC" y el dataset usado, con el período de cada cifra.
- Si `observaciones` dice "Dato provisorio" (u otro signo), aclaralo.
- Usá las variaciones publicadas (v_m, v_ia, v_acum) en lugar de calcularlas desde el índice: \
INDEC advierte diferencias por redondeo. Si calculás algo (ej. salario real), decilo explícitamente.
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


def create_server(mirror: Mirror) -> MCPServer:
    server = MCPServer(
        name="indec",
        title="INDEC Argentina (no oficial)",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
    def latest_indicators() -> dict[str, Any]:
        """Últimas cifras de titular del INDEC, cada una con su período: inflación (IPC nacional,
        general y núcleo), actividad (EMAE), salarios, canastas básicas CBA/CBT, pobreza e
        indigencia, desocupación y empleo. Usala primero para preguntas generales."""
        return mirror.snapshot()

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
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
    def list_datasets() -> list[dict[str, Any]]:
        """Los datasets del mirror (nombre de tabla SQL, descripción, filas, último período, estado
        de sincronización)."""
        return mirror.datasets()

    @server.tool(annotations=READ_ONLY)
    @_tool_errors
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
    def query_sql(
        sql: Annotated[str, Field(description="Una única consulta SELECT en dialecto DuckDB.")],
    ) -> dict[str, Any]:
        return mirror.query(sql)

    return server


def main() -> None:
    # Default to the repo's data/ dir so the server works regardless of the client's cwd.
    default = Path(__file__).resolve().parents[2] / "data" / "indec.duckdb"
    db = os.environ.get("INDEC_DB") or str(default)
    mirror = Mirror(db, Limits())
    create_server(mirror).run("stdio")


if __name__ == "__main__":
    main()
