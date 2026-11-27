# Responsibility: the kernel side of v5's AGENT brief (step 12): the involved entities' sizes and the gaps between them.
# Owns: the registered kernel function `v5_agent_region`.
# Boundaries: runs INSIDE a worker (OCP imported lazily). Measures only; what the agent is told is
#             `agent_entrants.py`'s. Reuses sites_kernel's box/extrema helpers and body_kernel's volume.
"""`v5_agent_region`: per entity its type, box size, area (faces), surface type (faces) and volume (solids); per pair
of entities (the first `MAX_PAIR_ENTITIES`) the exact distance, skipped when the call budget runs out."""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 30.0
MAX_ENTITIES = 12
MAX_PAIR_ENTITIES = 6


@register("v5_agent_region")
def v5_agent_region(payload: dict) -> dict:
    """`{"entities": {id: {type, size_mm, area_mm2?, surface?, volume_mm3?}}, "gaps": [{a, b, gap_mm}],
    "truncated"}` for `payload["entities"]` (refs `{id, type, index}`)."""
    from meshpipeline.cad.repair_v4.body_kernel import _volume
    from meshpipeline.cad.repair_v4.sites_kernel import _box, _extrema, _face_area, _sub
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep

    deadline = time.monotonic() + float(payload.get("budget_s", CALL_BUDGET_S))
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    subs: dict[str, Any] = {}
    out: dict[str, dict] = {}
    for ref in payload["entities"][:MAX_ENTITIES]:
        try:
            sub = _sub(maps, ref)
        except ValueError:
            continue
        subs[ref["id"]] = sub
        box = _box(sub)
        row: dict[str, Any] = {"type": ref["type"],
                               "size_mm": None if box is None else [round(box[i + 3] - box[i], 6) for i in range(3)]}
        if ref["type"] == "face":
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            from OCP.TopoDS import TopoDS

            row["area_mm2"] = round(_face_area(sub), 6)
            row["surface"] = str(BRepAdaptor_Surface(TopoDS.Face_s(sub)).GetType()).split(".")[-1].replace(
                "GeomAbs_", "")
        elif ref["type"] == "solid":
            row["volume_mm3"] = round(abs(_volume(sub)), 6)
        out[ref["id"]] = row
    gaps: list[dict] = []
    truncated = ""
    ids = list(subs)[:MAX_PAIR_ENTITIES]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if time.monotonic() > deadline:
                truncated = "over the call budget"
                break
            d = _extrema(subs[a], subs[b])["dist"]
            gaps.append({"a": a, "b": b, "gap_mm": None if d is None else round(d, 9)})
        if truncated:
            break
    return {"entities": out, "gaps": gaps, "truncated": truncated}
