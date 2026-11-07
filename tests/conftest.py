"""A fake PostgREST server mimicking the INDEC API behaviours the sync relies on."""

from __future__ import annotations

import json

import httpx
import pytest

PK_DOC = "Ordenamiento. Para paginar, incluya la clave completa: {}"


def make_spec(datasets: dict[str, list[str]]) -> dict:
    """datasets: path -> primary key columns."""
    paths = {}
    for path, pk in datasets.items():
        paths[path] = {
            "get": {
                "summary": f"Summary {path}",
                "description": f"Description {path}\n\n### Ejemplos\n\nignored",
                "tags": ["Dirección de Pruebas"],
                "parameters": [
                    {"name": "order", "in": "query", "description": PK_DOC.format(",".join(pk))},
                    {"name": "limit", "in": "query"},
                    {"name": "Anio", "in": "query",
                     "description": "Filtro para Año de referencia.   Use operadores como eq, gt, ilike."},
                ],
            }
        }
    return {"openapi": "3.1.0", "paths": paths}


class FakeIndec:
    def __init__(self, tables: dict[str, list[dict]], token: str = "tok", page_cap: int = 1000):
        self.tables = tables
        self.token = token
        self.page_cap = page_cap
        self.fail: dict[str, int] = {}  # path -> remaining 503s to return
        self.broken: set[str] = set()  # paths that always 500
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path.removeprefix("/v1")
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"code": "42501", "message": "permission denied for schema api"})
        if path in self.broken:
            return httpx.Response(500, json={"message": "boom"})
        if self.fail.get(path):
            self.fail[path] -= 1
            return httpx.Response(503, headers={"retry-after": "0"})
        rows = self.tables[path]
        limit = min(int(request.url.params.get("limit", self.page_cap)), self.page_cap)
        offset = int(request.url.params.get("offset", 0))
        page = rows[offset : offset + limit]
        headers = {"content-type": "application/json"}
        status = 200
        if request.headers.get("prefer") == "count=exact":
            end = offset + len(page) - 1
            headers["content-range"] = f"{offset}-{end}/{len(rows)}"
            if len(page) < len(rows):
                status = 206
        return httpx.Response(status, content=json.dumps(page), headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def export_rows(n: int) -> list[dict]:
    return [
        {"Anio": 2000 + i // 12, "Mes": i % 12 + 1, "Periodo": f"{2000 + i // 12}{i % 12 + 1:02d}",
         "pais_cod": "BR", "valor_FOB": float(i), "valor_FOB_null": None, "observaciones": None}
        for i in range(n)
    ]


@pytest.fixture
def no_sleep():
    return lambda _s: None
