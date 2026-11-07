"""Living conditions: basic baskets (CBA/CBT), the child-raising basket, poverty and the labor market.

Ideas adapted from SantinoSpingola/indec-mcp (MIT): dedicated tools instead of generic SQL. These
tables don't fit the generic series engine (pesos per household type, a wide-format table, and
semester/quarter EPH rates), so each gets a small purpose-built query.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from .mirror import SOURCE, Mirror, QueryError, format_period, parse_period

METHOD_POVERTY = "https://www.indec.gob.ar/ftp/cuadros/sociedad/EPH_metodologia_22_pobreza.pdf"


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def _range(start: str | None, end: str | None, table: str, default_months: int | None = 12) -> tuple[list[str], list]:
    """WHERE fragments for a period range; with no range, the last `default_months` months."""
    start, end = parse_period(start), parse_period(end)
    where, params = [], []
    if start:
        where.append("periodo >= ?")
        params.append(start)
    if end:
        where.append("periodo <= ?")
        params.append(end if len(end) != 4 else end + "99")  # '2025' -> include all of 2025
    if not start and not end and default_months:
        where.append(f"periodo >= (SELECT strftime(strptime(max(periodo), '%Y%m') - INTERVAL {default_months} "
                     f"MONTH, '%Y%m') FROM {table})")
    return where, params


# -- basic baskets ------------------------------------------------------------------------------

_CRIANZA_GROUPS = {"men1": "menores de 1 año", "1a3": "de 1 a 3 años", "4a5": "de 4 a 5 años", "6a12": "de 6 a 12 años"}
_CRIANZA_ALIASES = {
    "men1": "men1", "menores de 1": "men1", "menor de 1": "men1", "bebe": "men1", "0": "men1",
    "1a3": "1a3", "1 a 3": "1a3", "4a5": "4a5", "4 a 5": "4a5", "6a12": "6a12", "6 a 12": "6a12",
}


def basic_baskets(m: Mirror, basket: str = "CBT", age_group: str | None = None,
                  start: str | None = None, end: str | None = None) -> dict[str, Any]:
    key = _norm(basket or "cbt")
    if key in ("cbt", "total", "pobreza"):
        return _cba_cbt(m, "CBT", start, end)
    if key in ("cba", "alimentaria", "indigencia"):
        return _cba_cbt(m, "CBA", start, end)
    if key in ("crianza", "canasta de crianza", "ninez", "hijos"):
        return _crianza(m, age_group, start, end)
    raise QueryError("basket debe ser 'CBT' (línea de pobreza), 'CBA' (línea de indigencia) o 'crianza'.")


def _cba_cbt(m: Mirror, code: str, start, end) -> dict[str, Any]:
    t = "valorizacion_canastas_cba_cbt"
    where, params = _range(start, end, t)
    cond = " AND ".join(where) or "TRUE"
    rows = m._records(
        f"SELECT periodo, "
        f"max(valor) FILTER (WHERE codigo = ? AND tipo_de_indicador = ?) AS adulto_equivalente_ars, "
        f"max(valor) FILTER (WHERE codigo = ? AND tipo_de_indicador = 'Hogar 1') AS hogar_1_ars, "
        f"max(valor) FILTER (WHERE codigo = ? AND tipo_de_indicador = 'Hogar 2') AS hogar_2_ars, "
        f"max(valor) FILTER (WHERE codigo = ? AND tipo_de_indicador = 'Hogar 3') AS hogar_3_ars, "
        f"max(valor) FILTER (WHERE codigo = ? AND tipo_de_indicador = 'v_m') AS v_m, "
        f"max(valor) FILTER (WHERE codigo = ? AND tipo_de_indicador = 'v_ia') AS v_ia "
        f"FROM {t} WHERE {cond} GROUP BY periodo ORDER BY periodo",
        [code, code, f"{code} H1", f"{code} H2", f"{code} H3", code, code, *params],
    )
    if not rows:
        raise QueryError("No hay datos de canastas en ese período (la serie empieza en abril de 2016).")
    last = rows[-1]
    ratios = {
        h: round(last[f"{h}_ars"] / last["adulto_equivalente_ars"], 2)
        for h in ("hogar_1", "hogar_2", "hogar_3")
        if last.get(f"{h}_ars") and last.get("adulto_equivalente_ars")
    }
    for r in rows:
        r["periodo"] = format_period(r["periodo"])
    name = "Canasta Básica Total (CBT)" if code == "CBT" else "Canasta Básica Alimentaria (CBA)"
    line = "pobreza" if code == "CBT" else "indigencia"
    return {
        "canasta": name,
        "region": "Gran Buenos Aires",
        "rows": rows,
        "notas": [
            f"Valores mensuales en pesos. La {code} define la línea de {line}: un hogar es "
            f"{'pobre' if code == 'CBT' else 'indigente'} si su ingreso no alcanza su valor.",
            "Hogar 1, 2 y 3 son los hogares tipo del INDEC; en este período equivalen a "
            + ", ".join(f"{h.replace('_', ' ')} = {v} adultos equivalentes" for h, v in ratios.items())
            + f". El hogar 2 es el hogar tipo de 4 integrantes que suele citarse. Composición exacta: {METHOD_POVERTY}",
            "v_m y v_ia son las variaciones publicadas de la canasta por adulto equivalente.",
        ],
        "source": f"{SOURCE} Dataset {t}.",
    }


def _crianza(m: Mirror, age_group: str | None, start, end) -> dict[str, Any]:
    t = "canasta_crianza"
    if age_group:
        g = _CRIANZA_ALIASES.get(_norm(age_group).replace("años", "").replace("anos", "").strip())
        if g is None:
            raise QueryError(f"age_group debe ser uno de: {', '.join(_CRIANZA_GROUPS.values())}.")
        cols = (f"bys_{g} AS bienes_y_servicios_ars, cuid_{g} AS cuidado_ars, tot_{g} AS total_ars")
        label = f"Canasta de crianza, {_CRIANZA_GROUPS[g]}"
    else:
        cols = ", ".join(f"tot_{g} AS \"total_{g}_ars\"" for g in _CRIANZA_GROUPS)
        label = "Canasta de crianza: total por tramo de edad"
    where, params = _range(start, end, t)
    rows = m._records(f"SELECT periodo, {cols} FROM {t} WHERE {' AND '.join(where) or 'TRUE'} ORDER BY periodo", params)
    if not rows:
        raise QueryError("No hay datos de la canasta de crianza en ese período (la serie empieza en enero de 2020).")
    for r in rows:
        r["periodo"] = format_period(r["periodo"])
    return {
        "canasta": label,
        "region": "Gran Buenos Aires",
        "rows": rows,
        "notas": [
            "Costo mensual en pesos de criar a un niño o niña: bienes y servicios más la valorización del tiempo de "
            "cuidado. Tramos: " + "; ".join(f"{k} = {v}" for k, v in _CRIANZA_GROUPS.items()) + ".",
        ],
        "source": f"{SOURCE} Dataset {t}.",
    }


# -- poverty and labor market (EPH, 31 urban areas) ---------------------------------------------


def poverty(m: Mirror, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    t = "pobreza_eph"
    where, params = _range(start, end, t, default_months=None)
    rows = m._records(
        f"SELECT periodo, tasa_pobreza_personas AS pobreza_personas_pct, tasa_indigencia_personas AS "
        f"indigencia_personas_pct, tasa_pobreza_hogares AS pobreza_hogares_pct, tasa_indigencia_hogares AS "
        f"indigencia_hogares_pct, observaciones FROM {t} WHERE {' AND '.join(where) or 'TRUE'} ORDER BY periodo",
        params,
    )
    if not rows:
        raise QueryError("No hay datos de pobreza en ese período (semestral, desde el 2° semestre de 2016).")
    for r in rows:
        r["periodo"] = format_period(r["periodo"])
        if not r["observaciones"]:
            r.pop("observaciones")
    return {
        "indicador": "Incidencia de la pobreza y la indigencia (EPH)",
        "cobertura": "Total de 31 aglomerados urbanos; semestral",
        "rows": rows,
        "notas": [
            "Porcentaje de personas y de hogares con ingresos por debajo de la CBT (pobreza) o de la CBA (indigencia).",
            f"Metodología: {METHOD_POVERTY}",
        ],
        "source": f"{SOURCE} Dataset {t}.",
    }


_LABOR_COLS = {
    "tasa_de_actividad": "actividad_pct",
    "tasa_de_empleo": "empleo_pct",
    "tasa_de_desocupacion": "desocupacion_pct",
    "tasa_de_empleo_formal": "empleo_formal_pct",
    "tasa_de_empleo_informal": "empleo_informal_pct",
    "tasa_de_subocupacion": "subocupacion_pct",
    "tasa_de_ocupacion_demandante": "ocupados_demandantes_pct",
}


def labor_market(m: Mirror, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    t = "mercado_trabajo_eph"
    where, params = _range(start, end, t, default_months=None)
    cols = ", ".join(f"{c} AS {a}" for c, a in _LABOR_COLS.items())
    rows = m._records(
        f"SELECT periodo, {cols}, observaciones FROM {t} WHERE {' AND '.join(where) or 'TRUE'} ORDER BY periodo",
        params,
    )
    if not rows:
        raise QueryError("No hay datos del mercado de trabajo en ese período (trimestral, desde el 2° trimestre de 2016).")
    for r in rows:
        r["periodo"] = format_period(r["periodo"])
        if not r["observaciones"]:
            r.pop("observaciones")
    return {
        "indicador": "Principales tasas del mercado de trabajo (EPH)",
        "cobertura": "Total de 31 aglomerados urbanos; trimestral",
        "rows": rows,
        "notas": [
            "Actividad, empleo, empleo formal e informal: % de la población total. Desocupación, subocupación y "
            "ocupados demandantes: % de la población económicamente activa.",
        ],
        "source": f"{SOURCE} Dataset {t}.",
    }
