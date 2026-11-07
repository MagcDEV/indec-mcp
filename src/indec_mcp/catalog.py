"""Dataset registry parsed from INDEC's OpenAPI spec.

The spec is public (GET https://api.indec.gob.ar/v1/ needs no token) and carries everything
the sync needs: endpoint paths, the full primary key per table (required for stable
pagination), and a human description per column.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SPEC_SNAPSHOT = Path(__file__).resolve().parents[2] / "spec" / "indec-openapi.json"

# Query params PostgREST adds to every endpoint; not data columns.
_STD_PARAMS = {"select", "order", "limit", "offset", "Prefer"}
_PK_MARKER = "clave completa:"


@dataclass(frozen=True)
class Dataset:
    path: str  # API path, e.g. "/canasta-crianza"
    table: str  # DuckDB table name, e.g. "canasta_crianza"
    summary: str
    description: str
    directorate: str  # INDEC directorate (OpenAPI tag)
    primary_key: tuple[str, ...]  # column names as the API spells them
    column_docs: dict[str, str] = field(default_factory=dict)  # api column -> description


def table_name(path: str) -> str:
    """API path -> snake_case SQL identifier."""
    return re.sub(r"[^a-z0-9]+", "_", path.strip("/").lower()).strip("_")


def column_name(api_column: str) -> str:
    """API column -> normalized lowercase name (API mixes `Anio` and `anio`)."""
    return api_column.lower()


def _clean_column_doc(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^Filtro para\s+", "", text)
    text = re.sub(r"\s*Use operadores como.*$", "", text)
    return text.rstrip(". ")


def parse_spec(spec: dict) -> list[Dataset]:
    datasets = []
    for path, ops in spec["paths"].items():
        op = ops["get"]
        params = {p["name"]: p for p in op.get("parameters", [])}
        order_doc = params.get("order", {}).get("description", "")
        if _PK_MARKER not in order_doc:
            raise ValueError(f"{path}: no primary key in the 'order' parameter description")
        pk = tuple(c.strip() for c in order_doc.split(_PK_MARKER, 1)[1].split(",") if c.strip())
        datasets.append(
            Dataset(
                path=path,
                table=table_name(path),
                summary=op.get("summary", ""),
                description=op.get("description", "").split("### ")[0].strip(),
                directorate=(op.get("tags") or [""])[0],
                primary_key=pk,
                column_docs={
                    name: _clean_column_doc(p.get("description", ""))
                    for name, p in params.items()
                    if name not in _STD_PARAMS
                },
            )
        )
    tables = [d.table for d in datasets]
    if len(set(tables)) != len(tables):
        raise ValueError(f"table name collision: {tables}")
    return datasets


def load_snapshot(path: Path = SPEC_SNAPSHOT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
