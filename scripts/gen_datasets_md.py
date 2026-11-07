#!/usr/bin/env python3
"""Generate docs/datasets.md from the INDEC OpenAPI spec snapshot.

Usage: python3 scripts/gen_datasets_md.py [spec/indec-openapi.json] > docs/datasets.md
"""
import json
import re
import sys

STD_PARAMS = {"select", "order", "limit", "offset", "Prefer"}


def main(path: str) -> None:
    spec = json.load(open(path, encoding="utf-8"))
    out = [
        "# INDEC API datasets",
        "",
        "Generated from `spec/indec-openapi.json` by `scripts/gen_datasets_md.py`. Do not edit by hand.",
        "",
        f"Base URL: `{spec['servers'][0]['url']}`. {len(spec['paths'])} endpoints, all `GET`, all require a Bearer JWT.",
        "",
    ]
    by_tag: dict[str, list[tuple[str, dict]]] = {}
    for p, ops in spec["paths"].items():
        op = ops["get"]
        by_tag.setdefault(op["tags"][0], []).append((p, op))

    for tag, items in by_tag.items():
        out += [f"## {tag}", ""]
        for p, op in items:
            desc = op.get("description", "")
            first = desc.split("\n")[0].strip()
            pk = re.search(r"Clave primaria\s*\n+\s*Estas columnas[^:]*:\s*(.+)", desc)
            cols = [x["name"] for x in op.get("parameters", []) if x["name"] not in STD_PARAMS]
            out += [
                f"### `{p}` — {op.get('summary', '')}",
                "",
                first,
                "",
                f"- **Primary key:** {pk.group(1).strip() if pk else 'n/a'}",
                f"- **Columns:** {', '.join(f'`{c}`' for c in cols)}",
                "",
            ]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "spec/indec-openapi.json")
