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


def _quality(row: dict, stats: dict) -> list[str]:
    """The measured quality numbers, not just the mesh size.

    Cell counts alone cannot answer "is this mesh any good", and the evidence JSON these lines
    are drawn from lives under the gate's scratch directory, which is removed unless a tier
    failed. Whatever is omitted here is therefore gone once the runner dies - so the measured
    quality travels in the summary rather than only in a file nobody will have.
    """
    bits: list[str] = []
    for key, label in (("max_non_ortho", "non_ortho"), ("max_skewness", "skew")):
        value = stats.get(key, row.get(key))
        if value is not None:
            bits.append(f"{label}={value}")
    deviation = stats.get("surface_deviation", row.get("surface_deviation"))
    if isinstance(deviation, dict):
        # The ratio a fidelity claim rests on; the rest of the block stays in the JSON.
        for key in ("p95_ratio", "max_ratio"):
            if deviation.get(key) is not None:
                bits.append(f"surf_dev_{key}={deviation[key]}")
    elif deviation is not None:
        bits.append(f"surf_dev={deviation}")
    return bits


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
    bits.extend(_quality(row, stats))
    return " ".join(bits)


def main(path: str) -> int:
    rows = _rows(json.load(open(path)))
    print(" | ".join(_describe(r) for r in rows if isinstance(r, dict)) or "no rows recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
