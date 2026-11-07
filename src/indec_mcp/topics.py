"""Intent-level helpers for foreign trade and population, on top of `Mirror`.

Ideas adapted from SantinoSpingola/indec-mcp (MIT): resolving trade codes by name, and always
reporting rows hidden by statistical secrecy instead of silently summing around them.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .mirror import SOURCE, Mirror, QueryError, format_period, parse_period

# -- foreign trade ----------------------------------------------------------------------------

FLOWS = {
    "exportaciones": {
        "table": "comercio_exterior_exportacion_bienes",
        "value": "valor_fob",
        "value_label": "FOB",
        "category": "rubro_cod",
        "subcategory": "subrubro_cod",
    },
    "importaciones": {
        "table": "comercio_exterior_importacion_bienes",
        "value": "valor_cif",
        "value_label": "CIF",
        "category": "uso_economico_cod",
        "subcategory": "gce_cod",
    },
}
_FLOW_ALIASES = {
    "exportaciones": "exportaciones", "exportacion": "exportaciones", "expo": "exportaciones",
    "exports": "exportaciones", "x": "exportaciones",
    "importaciones": "importaciones", "importacion": "importaciones", "impo": "importaciones",
    "imports": "importaciones", "m": "importaciones",
}
CODE_KINDS = {
    "pais_cod": "país",
    "rubro_cod": "rubro de exportación",
    "subrubro_cod": "subrubro de exportación",
    "uso_economico_cod": "uso económico de importación",
    "gce_cod": "gran categoría económica de importación",
}
GROUPS = ("mes", "anio", "pais", "categoria", "subcategoria")
CONFIDENTIAL_NOTE = (
    "Las sumas son CALCULADAS por este servicio y EXCLUYEN las filas con secreto estadístico "
    "('s', valor no publicado a este nivel de detalle). Los totales oficiales del INDEC sí las "
    "incluyen, así que pueden ser mayores. Mencioná esta salvedad al citar montos."
)

_NORM = "lower(strip_accents({}))"


def _norm(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def trade_codes(m: Mirror, query: str, kinds: list[str] | None = None, limit: int = 25) -> list[dict]:
    """Search the trade code dictionary by name or code (accent-insensitive)."""
    kinds = kinds or list(CODE_KINDS)
    q = query.strip()
    rows = m._records(
        "SELECT nombre_columna, codigo, codigo_descripcion FROM comercio_exterior_nomenclaturas "
        f"WHERE list_contains(?, nombre_columna) AND ({_NORM.format('codigo_descripcion')} LIKE ? "
        "OR upper(codigo) = upper(?)) "
        f"ORDER BY ({_NORM.format('codigo_descripcion')} = ?) DESC, length(codigo_descripcion), codigo LIMIT ?",
        [kinds, f"%{_norm(q)}%", q, _norm(q), limit],
    )
    return [
        {"tipo": CODE_KINDS[r["nombre_columna"]], "columna": r["nombre_columna"],
         "codigo": r["codigo"], "descripcion": r["codigo_descripcion"]}
        for r in rows
    ]


# Common names that don't match INDEC's official country names (codes verified in the dictionary).
_COUNTRY_ALIASES = {
    "eeuu": "US", "ee.uu.": "US", "ee uu": "US", "usa": "US", "estados unidos de america": "US",
    "uk": "GB", "inglaterra": "GB", "gran bretana": "GB", "corea del sur": "KR", "corea": "KR",
    "corea del norte": "KP", "holanda": "NL", "rusia": "RU", "emiratos": "AE", "chequia": "CZ",
    "vietnam": "VN",
}


def _resolve_code(m: Mirror, query: str, kinds: list[str], what: str) -> dict:
    if "pais_cod" in kinds and _norm(query) in _COUNTRY_ALIASES:
        query = _COUNTRY_ALIASES[_norm(query)]
    found = trade_codes(m, query, kinds, limit=30)
    q = _norm(query)
    # Exact name or code first; then an acronym in parentheses ("MOA" -> "... AGROPECUARIO (MOA)").
    # Order matters: "China" must not also match "Hong Kong ... (China)".
    exact = [f for f in found if _norm(f["descripcion"]).strip() == q or f["codigo"].upper() == query.strip().upper()]
    if not exact:
        exact = [f for f in found if f"({q})" in _norm(f["descripcion"])]
    if len(exact) == 1:
        return exact[0]
    if len(found) == 1:
        return found[0]
    if not found:
        raise QueryError(f"No encontré {what} {query!r}. Probá `trade_codes` para buscar el nombre exacto.")
    opts = "; ".join(f"{f['descripcion']} ({f['tipo']}, {f['codigo']})" for f in found[:15])
    raise QueryError(f"{query!r} es ambiguo. Opciones: {opts}")


def foreign_trade(
    m: Mirror,
    flow: str = "exportaciones",
    country: str | None = None,
    category: str | None = None,
    start: str | None = None,
    end: str | None = None,
    group_by: str = "mes",
    top: int = 15,
) -> dict:
    fkey = _FLOW_ALIASES.get(_norm(flow))
    if fkey is None:
        raise QueryError("flow debe ser 'exportaciones' o 'importaciones'.")
    if group_by not in GROUPS:
        raise QueryError(f"group_by debe ser uno de: {', '.join(GROUPS)}.")
    f = FLOWS[fkey]
    t, v = f["table"], f["value"]
    top = max(1, min(int(top), 50))

    where, params, filters = [], [], {}
    if country:
        c = _resolve_code(m, country, ["pais_cod"], "el país")
        where.append("t.pais_cod = ?")
        params.append(c["codigo"])
        filters["pais"] = c["descripcion"]
    if category:
        c = _resolve_code(m, category, [f["category"], f["subcategory"]], "la categoría")
        where.append(f"t.{c['columna']} = ?")
        params.append(c["codigo"])
        filters["categoria"] = f"{c['descripcion']} ({c['tipo']})"

    start, end = parse_period(start), parse_period(end)
    for p in (start, end):
        if p is not None and not re.fullmatch(r"\d{6}|\d{4}", p):
            raise QueryError("Usá períodos 'AAAA-MM' o 'AAAA'.")
    if start and len(start) == 4:
        start += "01"
    if end and len(end) == 4:
        end += "12"
    if not start and not end:  # default: last 12 months available
        where.append(
            f"t.periodo > (SELECT strftime(strptime(max(periodo), '%Y%m') - INTERVAL 12 MONTH, '%Y%m') FROM {t})"
        )
    if start:
        where.append("t.periodo >= ?")
        params.append(start)
    if end:
        where.append("t.periodo <= ?")
        params.append(end)
    cond = " AND ".join(where) or "TRUE"

    def name_of(col: str) -> str:
        return (f"coalesce((SELECT codigo_descripcion FROM comercio_exterior_nomenclaturas n "
                f"WHERE n.nombre_columna = '{col}' AND n.codigo = t.{col}), t.{col})")

    group_expr = {
        "mes": "t.periodo", "anio": "CAST(t.anio AS VARCHAR)",
        "pais": name_of("pais_cod"), "categoria": name_of(f["category"]), "subcategoria": name_of(f["subcategory"]),
    }[group_by]
    by_time = group_by in ("mes", "anio")
    agg = (f"round(sum(t.{v}) / 1e6, 2) AS millones_usd, round(sum(t.peso_neto) / 1e6, 1) AS miles_toneladas, "
           f"count(*) FILTER (WHERE t.{v} IS NULL) AS filas_confidenciales, count(*) AS filas")
    rows = m._records(
        f"SELECT {group_expr} AS grupo, {agg} FROM {t} t WHERE {cond} GROUP BY 1 "
        f"ORDER BY {'grupo' if by_time else 'millones_usd DESC NULLS LAST'} LIMIT {200 if by_time else top}",
        params,
    )
    total = m._records(
        f"SELECT min(t.periodo) AS desde, max(t.periodo) AS hasta, {agg} FROM {t} t WHERE {cond}", params
    )[0]
    if not total["filas"]:
        raise QueryError("No hay datos para esos filtros y período.")
    for r in rows:
        if group_by == "mes":
            r["grupo"] = format_period(r["grupo"])
    share = total["filas_confidenciales"] / total["filas"]
    return {
        "flujo": f"{fkey} de bienes (valor {f['value_label']}, millones de USD)",
        "filtros": filters,
        "desde": format_period(total["desde"]),
        "hasta": format_period(total["hasta"]),
        "agrupado_por": group_by,
        "rows": rows,
        "total": {k: total[k] for k in ("millones_usd", "miles_toneladas", "filas_confidenciales", "filas")},
        "porcentaje_filas_confidenciales": round(share * 100, 1),
        "nota": CONFIDENTIAL_NOTE,
        "source": f"{SOURCE} Dataset {t}.",
    }


# -- population -------------------------------------------------------------------------------

_JUR_ALIASES = {
    # Not "capital": it is also a department name in Córdoba, Catamarca, Salta and others.
    "caba": "02", "capital federal": "02", "ciudad de buenos aires": "02",
    "provincia de buenos aires": "06", "pba": "06", "tierra del fuego": "94",
}
_COUNTRY = {"", "argentina", "pais", "total pais", "total del pais", "nacional", "total"}


def population(m: Mirror, place: str | None = None, year: int | None = None, by: str | None = None) -> dict:
    """Population for the country, a province (jurisdicción) or a department, by sex/age."""
    year = int(year or dt.date.today().year)
    by = (by or "sexo").lower()
    if by not in ("sexo", "edad", "departamento"):
        raise QueryError("by debe ser 'sexo', 'edad' o 'departamento'.")
    key = _norm(place or "")
    note = "Estimaciones y proyecciones de población al 1° de julio, base Censo 2022."

    if key in _COUNTRY:
        _check_year(year, 2022, 2040)
        t = "proyecciones_poblacion_nacional_2022_2040"
        if by == "departamento":
            raise QueryError("Para departamentos indicá una provincia, ej. place='Córdoba', by='departamento'.")
        group = "sexo_nom" if by == "sexo" else "CASE WHEN edad >= 100 THEN '100 y más' ELSE " \
            "CAST(edad // 5 * 5 AS VARCHAR) || ' a ' || CAST(edad // 5 * 5 + 4 AS VARCHAR) END"
        order = "1" if by == "sexo" else "min(edad)"
        rows = m._records(
            f"SELECT {group} AS grupo, sum(poblacion) AS poblacion FROM {t} WHERE fecha = ? GROUP BY 1 ORDER BY {order}",
            [year], max_rows=200,
        )
        label, level = "Argentina", "país"

    else:
        jur = _resolve_jurisdiction(m, key)
        if jur is not None and by != "departamento":
            _check_year(year, 2022, 2040)
            t = "proyecciones_poblacion_jurisdiccion_2022_2040"
            group, order = ("sexo_nom", "1") if by == "sexo" else ("edad_nom", "min(CAST(edad_cod AS INTEGER))")
            rows = m._records(
                f"SELECT {group} AS grupo, sum(poblacion) AS poblacion FROM {t} "
                f"WHERE jurisdiccion_cod = ? AND fecha = ? GROUP BY 1 ORDER BY {order}",
                [jur["cod"], str(year)], max_rows=200,
            )
            label, level = jur["nom"], "provincia"
        elif jur is not None:  # departments of a province
            _check_year(year, 2022, 2035)
            t = "estimaciones_poblacion_departamento_2022_2035"
            rows = m._records(
                f"SELECT departamento_nom AS grupo, sum(poblacion) AS poblacion FROM {t} "
                "WHERE jurisdiccion_cod = ? AND fecha = ? GROUP BY 1 ORDER BY 2 DESC",
                [jur["cod"], str(year)], max_rows=200,
            )
            label, level = jur["nom"], "provincia (por departamento)"
        else:
            if by == "edad":
                raise QueryError("La apertura por edad existe para el país y las provincias, no por departamento.")
            _check_year(year, 2022, 2035)
            t = "estimaciones_poblacion_departamento_2022_2035"
            dep = _resolve_department(m, place or "")
            rows = m._records(
                f"SELECT sexo_nom AS grupo, sum(poblacion) AS poblacion FROM {t} "
                "WHERE departamento_cod = ? AND fecha = ? GROUP BY 1 ORDER BY 1",
                [dep["departamento_cod"], str(year)], max_rows=10,
            )
            label, level = f"{dep['departamento_nom']} ({dep['jurisdiccion_nom']})", "departamento"

    if not rows:
        raise QueryError("No hay datos para ese lugar y año.")
    return {
        "lugar": label,
        "nivel": level,
        "anio": year,
        "total": sum(r["poblacion"] for r in rows),
        "apertura": by,
        "rows": rows,
        "nota": note,
        "source": f"{SOURCE} Dataset {t}.",
    }


def _check_year(year: int, lo: int, hi: int) -> None:
    if not lo <= year <= hi:
        raise QueryError(f"Año fuera de rango: hay datos de {lo} a {hi}.")


def _resolve_jurisdiction(m: Mirror, key: str) -> dict | None:
    jurs = m._records(
        "SELECT DISTINCT jurisdiccion_cod AS cod, jurisdiccion_nom AS nom "
        "FROM proyecciones_poblacion_jurisdiccion_2022_2040", max_rows=100,
    )
    if key in _JUR_ALIASES:
        return next(j for j in jurs if j["cod"] == _JUR_ALIASES[key])
    exact = [j for j in jurs if _norm(j["nom"]) == key]
    if exact:
        return exact[0]
    return None


def _resolve_department(m: Mirror, place: str) -> dict:
    name, _, prov = place.partition(",")
    rows = m._records(
        "SELECT DISTINCT departamento_cod, departamento_nom, jurisdiccion_nom "
        "FROM estimaciones_poblacion_departamento_2022_2035 "
        f"WHERE {_NORM.format('departamento_nom')} LIKE ? "
        f"AND (? = '' OR {_NORM.format('jurisdiccion_nom')} LIKE ?) ORDER BY 3, 2 LIMIT 30",
        [f"%{_norm(name)}%", _norm(prov), f"%{_norm(prov)}%"], max_rows=30,
    )
    exact = [r for r in rows if _norm(r["departamento_nom"]) == _norm(name)]
    pick = exact if exact else rows
    if len(pick) == 1:
        return pick[0]
    if not pick:
        raise QueryError(f"No encontré la provincia o departamento {place!r}.")
    opts = "; ".join(f"{r['departamento_nom']}, {r['jurisdiccion_nom']}" for r in pick[:15])
    raise QueryError(f"{place!r} es ambiguo. Indicá 'Departamento, Provincia'. Opciones: {opts}")
