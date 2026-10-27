# Responsibility: Condense one engine-benchmark evidence file into a single line for the gate log.
# Boundaries: pure stdlib, reads JSON and prints text; it measures nothing and asserts nothing.
from __future__ import annotations

import json
import sys


def _rows(doc: object) -> list[dict]:
    if isinstance(doc, dict):
        results = doc.get("results")
        return results if isinstance(results, list) else []
    return doc if isinstance(doc, list) else []


def _describe(row: dict) -> str:
    label = str(row.get("case") or row.get("engine") or "?")
    variant = row.get("variant")
    if variant:
        label = label + "/" + str(variant)
    bits = [label, "mesh=" + ("ok" if row.get("status") == "success" else "no")]
    repair = (row.get("cad_repair") or {}).get("status")
    if repair:
        bits.append("repair=" + str(repair))
    stats = row.get("mesh_statistics") or {}
    cells = stats.get("cells", row.get("cells"))
    if cells:
        bits.append("cells=" + str(cells))
    return " ".join(bits)


def main(path: str) -> int:
    rows = _rows(json.load(open(path)))
    print(" | ".join(_describe(r) for r in rows if isinstance(r, dict)) or "no rows recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
