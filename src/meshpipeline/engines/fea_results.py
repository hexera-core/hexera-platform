# Responsibility: Say what a delivered FEA mesh is, the way a structural engineer reads it (the V1 result screen, design 3.8): its elements and quality in plain words against each limit, the through-wall check, the units, the plan in words, the hot spots, every named face, and what the download holds.
# Boundaries: a reader over the workspace's own records (quality.json, fea_plan.json); it measures nothing and decides no verdict - the limits it lists are the ones the verdict reads (engines/verdict_limits.fea_limits). Engine-neutral: any FEA adapter writes the same records.
# Collaborates with: engines/gmsh/spec.py (the gmsh viewer hook attaches it as the payload's `fea` block), engines/fea_quality.py and engines/fea_checks.py (the numbers), ui/js/viewer/fea_results.js (draws it).
"""THE FEA RESULT BLOCK (the viewer payload's `fea`).

    {"title", "summary", "units": {"mesh", "words"}, "level",
     "check": {"outcome": "pass" | "concerns" | "fail", "words", "rows": [{key, label, measured,
               limit, op, ok, words}]},
     "through_wall": {"ok", "min", "needed", "words", "short": [{face, thickness_mm, elements}]},
     "plan_words": [...], "hot_spots": [{id, shape, words, faces, at_m, capped, per_quarter_turn}],
     "faces": [{name, cad_name, kind, area_mm2, label}],
     "download": {"words", "contains": [...], "later": "..."},
     "solvers": [{solver, label, file, element, tested, tested_words, outcome, words, rows, file_ready}],
     "assembly": {"connection", "words", "parts": [{id, name, elements}], "joints": [{name, kind, words}],
                  "notes"}  (an assembly only)}
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: the solver files the download holds today, and what is next (design 8: step 6 writes the rest)
CONTAINS = (
    ("mesh.inp", "CalculiX / Abaqus input deck: 10-node tetrahedra (C3D10), every CAD face a *SURFACE "
                 "and a *NSET named Face_k"),
    ("mesh.bdf", "Nastran bulk data to INCLUDE in your run deck: 10-node CTETRA, every CAD face a SET3 of "
                 "its grids and a BSURFS of its element faces, numbered k"),
    ("mesh.cdb", "Ansys MAPDL archive: SOLID187, every CAD face a node component FACE_k"),
    ("fea_plan.json", "the mesh plan: every size and the rule it came from"),
    ("geometry.step", "the CAD the mesh was built from, in metres"),
)
SAME_NUMBERS = ("Every file has the same node and element numbers and the same Face_k, and its lengths in "
                "metres. Each solver file is also its own download above: pick the one for your solver.")


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _fmt(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return "not measured"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.3g}" if math.isfinite(v) else "not measured"
    return str(v)


def _shape_words(h: Mapping) -> str:
    shape = str(h.get("shape") or h.get("type") or "hot spot")
    if h.get("radius_mm") is not None:
        return f"{shape}, radius {float(h['radius_mm']):.3g} mm"
    if h.get("angle_deg") is not None:
        return f"sharp {shape}, {float(h['angle_deg']):.0f} degrees" + (" (capped)" if h.get("capped") else "")
    return shape


def _face_key(name: str) -> tuple:
    """Face_2 before Face_10, Part_1_Face_9 before Part_2_Face_1."""
    import re

    return tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(name)))


#: How an assembly's joints are said, by kind.
JOINT_WORDS = {
    "bonded": "bonded: one mesh, the parts share their nodes on {slave} / {master}",
    "contact": "contact pair: {slave} (slave) on {master} (master)",
    "tie": "tied: {slave} (slave) to {master} (master)",
}


def viewer_face_names(workspace) -> dict[int, str] | None:
    """An assembly's mesh surfaces by name for the viewer (quality.json's fea.assembly.face_tags):
    surface tag -> Part_k_Face_j, a face two parts share named by the lower-numbered part's (the
    other side is inside the mesh); None for a single part."""
    q = _read(Path(workspace) / "quality.json")
    tags = ((q.get("fea") or {}).get("assembly") or {}).get("face_tags") or {}
    if not tags:
        return None
    out: dict[int, str] = {}
    for name in sorted(tags, key=_face_key):
        for t in tags[name]:
            out.setdefault(int(t), str(name))
    return out


def assembly_block(asm: Mapping) -> dict | None:
    """An assembly's lines on the result screen: its parts with their element counts, how they are
    joined and every joint as written (PLAN-fea-17); None for a single part."""
    if not asm:
        return None
    mode = str(asm.get("mode") or "")
    joints = [{"name": j.get("name"), "kind": j.get("kind"), "parts": j.get("parts"),
               "words": JOINT_WORDS.get(str(j.get("kind")), "{slave} / {master}").format(
                   slave=j.get("slave"), master=j.get("master"))
               + (" - they start overlapping" if j.get("overlap") else "")} for j in asm.get("joints") or []]
    words = {"bonded": "Bonded (welded or glued): one conformal mesh - the parts share their nodes where they touch.",
             "contact": "Contact: each part meshed on its own, with a named contact pair wherever two parts touch.",
             "tie": "Bonded, tied: each part meshed on its own and tied where they touch (the parts could not be "
                    "fused into one mesh)."}.get(mode, "")
    return {"connection": asm.get("connection"), "mode": mode, "words": words,
            "parts": [{"id": p.get("id"), "name": p.get("name") or "", "elements": p.get("elements")}
                      for p in asm.get("parts") or []],
            "joints": joints, "absorbed": list(asm.get("absorbed") or []), "notes": list(asm.get("notes") or [])}


#: Said on the result screen of an FEA mesh whose 3D view could not be built.
FEA_VIEW_MISSING = ("The 3D view could not be built for this mesh, so it is not shown. The mesh is valid and "
                    "its files are complete - download them below.")
#: Said beside a solver whose file could not be written: the deck and the other files are complete.
FILE_MISSING = "This solver file isn't available for this mesh; the other files are complete."


def _ready(ws: Path, line: dict) -> dict:
    ready = (ws / line["file"]).exists()
    return {**line, "file_ready": ready, **({} if ready else {"file_note": FILE_MISSING})}


def results(workspace) -> dict | None:
    """The block, or None for a workspace that holds no FEA plan."""
    from meshpipeline.contracts.decision_record import limit_met
    from meshpipeline.engines.fea_solvers import judge
    from meshpipeline.engines.verdict_limits import fea_limits, words

    ws = Path(workspace)
    plan = _read(ws / "fea_plan.json")
    q = _read(ws / "quality.json")
    if not plan or not (q.get("fea") or {}).get("plan"):
        return None
    fea = q.get("fea") or {}
    rules = fea.get("rules") or {}
    cells = int(q.get("cells") or 0)
    level = "Recommended"

    # THE CHECK: the one gating row (validity), then every note row - the same rows, the same limits,
    # as the job's verdict reads (engines/verdict_limits.fea_limits), so the two never disagree
    tw = fea.get("through_wall_min")
    gating = [
        {"key": "fea_inverted", "label": "Inverted elements (a Jacobian not above zero)", "op": "==",
         "limit": 0, "measured": q.get("fea_inverted")},
    ]
    rows = []
    for r in gating + fea_limits(q):
        met = limit_met(r.get("measured"), str(r.get("op")), r.get("limit"))
        # A VALID MESH IS THE ONLY BLOCKER: inverted elements fail; the floor and the walls are notes
        held = r in gating and r["key"] == "fea_inverted"
        rows.append({**{k: r.get(k) for k in ("key", "label", "op", "limit", "measured")}, "ok": met,
                     "gating": held, "words": words(r)})
    if any(r["ok"] is False and r["gating"] for r in rows) or q.get("fatal"):
        outcome, said = "fail", "The mesh fails a check no solver can work around."
    elif any(r["ok"] is False for r in rows):
        outcome, said = "concerns", "Passed, with notes: the mesh is valid; some figures are past a limit (listed)."
    elif any(r["ok"] is None for r in rows):
        # NOT MEASURED IS NOT MET: a figure missing (an older runner, a measurement that failed) is
        # never said to pass
        outcome, said = "unmeasured", ("Not fully checked: some figures were not measured on this mesh "
                                        "(listed). The ones measured meet their limits.")
    else:
        outcome, said = "pass", "Every element meets the strictest limit of the solvers it is for."

    short = (rules.get("through_wall") or {}).get("short") or []
    through = {"ok": tw is not None and float(tw) >= 1.8, "min": tw, "needed": 2,
               "short": [{"face": s.get("face"), "thickness_mm": s.get("thickness_mm"), "elements": s.get("elements")}
                         for s in short],
               "words": (f"Yes - at least {float(tw):.3g} elements through every wall (2 needed)."
                         if tw is not None and float(tw) >= 1.8 else
                         ("No - " + ", ".join(f"{s.get('face')} has {s.get('elements')} through a "
                                              f"{s.get('thickness_mm')} mm wall" for s in short[:5])
                          if short else "Not measured on this mesh."))}

    spot_rows = {s.get("spot"): s for s in (rules.get("hot_spots") or {}).get("short") or []}
    spots = []
    for h in plan.get("hot_spots") or []:
        row = {"id": h.get("id"), "type": h.get("type"), "shape": h.get("shape"), "faces": h.get("faces") or [],
               "at_m": h.get("at_m"), "capped": bool(h.get("capped")), "words": _shape_words(h)}
        if h.get("id") in spot_rows:
            row["short"] = spot_rows[h["id"]]
        spots.append(row)

    # THE FACES THE DECK NAMES are the list (its face sets), each with what the plan measured of it
    deck = q.get("deck") or {}
    names = {str(k): str(v) for k, v in (deck.get("cad_face_names") or {}).items()}
    measured = {str(f.get("name")): f for f in plan.get("faces") or []}
    listed = list((deck.get("face_sets") or {}).keys()) or list(measured)
    faces = []
    for n in sorted(listed, key=_face_key):
        f = measured.get(n) or {}
        cad = names.get(n) or names.get(n.rsplit("_", 1)[-1]) or ""
        faces.append({"name": n, "cad_name": cad, "kind": f.get("kind"), "area_mm2": f.get("area_mm2"),
                      "label": f"{n} · {cad}" if cad else n})

    # one authority for a delivered mesh's unit (contracts/mesh_units): every deck is written in it
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    mesh_units = COMPLETED_MESH_UNIT.value
    asm = assembly_block(fea.get("assembly") or {})
    extra = {"assembly": asm} if asm else {}
    return {**extra,
        "title": "FEA mesh",
        "summary": (f"{cells:,} quadratic tetrahedra (10-node), a solid mesh"
                    + (f" of {len(asm['parts'])} parts" if asm else "")
                    + (f", {level} level" if level else "") + f"; {len(faces)} named faces."),
        "level": level,
        "units": {"mesh": mesh_units,
                  "words": (f"Lengths in the files are in {'metres' if mesh_units == 'm' else mesh_units} "
                            "(the part's own unit, converted). Use consistent units for loads and materials "
                            "in your solver (for example N, m, Pa).")},
        "check": {"outcome": outcome, "words": said, "rows": rows,
                  "solvers": "Limits: the strictest of Abaqus/CalculiX, Nastran and Ansys."},
        "elements": cells,
        "through_wall": through,
        "plan_words": list(plan.get("words") or []),
        "hot_spots": spots,
        "faces": faces,
        "download": {"words": "The full download holds:",
                     "contains": [{"file": f, "words": w} for f, w in CONTAINS if (ws / f).exists()],
                     "later": SAME_NUMBERS},
        # PASS / WARNINGS / FAIL PER SOLVER, by each solver's own published checks
        # (engines/fea_solvers.py), with its file and whether that solver has really run it
        "solvers": [_ready(ws, line) for line in judge({**q, **(fea.get("quality") or {})})],
    }


__all__ = ["CONTAINS", "JOINT_WORDS", "SAME_NUMBERS", "assembly_block", "results", "viewer_face_names"]
