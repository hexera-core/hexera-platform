# Responsibility: Say, from the measurements alone, whether a delivered FEA mesh is valid, and put the measured facts a structural reviewer judges in plain lines.
# Boundaries: pure readers over the job's saved purpose, the mesh manifest's quality block and the workspace's own records (quality.json, fea_plan.json). It measures nothing and decides no delivery; the review policy and the run's outcome read the validity, the reviewer's brief reads the facts.
# Collaborates with: agents/reviewer/review_policy.py (a review never fails a mesh this calls valid), application/final_result.py (the same rule at the run's end), agents/reviewer/context.py (the facts in the brief), engines/fea_results.py (the result screen's check, read for the brief), engines/gmsh/fea_adapter.py (writes the records).
"""THE FEA REVIEW'S MEASURED GROUND.

An FEA job is the one whose SAVED PURPOSE (the intake's purpose mapping, carried on the job as
`purpose`) is structural. For it the hard pass/fail is the measured checks': a mesh with no
inverted element and nothing fatal is VALID, and a review may explain it and add concerns but can
never fail it (the real-path test of 2026-10-22: 14 of 24 valid FEA meshes were failed as "the
wrong problem" because the viewer showed one surface group, "free", while every solver file named
each CAD face on its own).

The reviewer is shown the measured facts it judges by, in plain lines: the named faces in each
solver file against the CAD's faces, the element count and type per file, the through-wall count,
the hot spots, what a coarsened plan left short and what was set aside at a cusp.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The saved purpose of an FEA job (engines/purposes.py, application/purpose_map's FEA route).
FEA_PURPOSE = "structural"

#: Each solver file and its element, in the download's order (engines/fea_writers.SOLVERS).
SOLVER_FILES = (("CalculiX / Abaqus", "mesh.inp", "C3D10"),
                ("Nastran", "mesh.bdf", "CTETRA (10-node)"),
                ("Ansys MAPDL", "mesh.cdb", "SOLID187"))

#: Why the viewer shows one group while the files name every face - said to the reviewer so a
#: single preview group is never read as a missing decomposition.
ONE_PREVIEW_GROUP = (
    "The 3D preview shows the whole outer surface as ONE group (named 'free'): that is the preview "
    "only. The per-face groups live in the solver files, where every CAD face is its own named set "
    "Face_k (counted below). A single preview group is not a missing face decomposition.")


def is_fea(purpose: Any) -> bool:
    """Whether the job's saved purpose is FEA (structural)."""
    return str(purpose or "").strip() == FEA_PURPOSE


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def measured_valid(quality: Mapping | None) -> bool | None:
    """THE MEASURED VALIDITY of an FEA mesh, from the manifest's quality block: True with no
    inverted element and nothing fatal, False with either, None when the block carries no FEA
    measurement (not an FEA mesh, or the element check never ran) - an unmeasured mesh is never
    said to be valid."""
    q = _m(quality)
    inverted = _num(q.get("fea_inverted"))
    if inverted is None:
        inverted = _num(_m(_m(q.get("fea")).get("quality")).get("fea_inverted"))
    if q.get("fatal"):
        return False
    if inverted is None:
        return None
    return inverted == 0


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _m(value) -> Mapping:
    """`value` when it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def facts(workspace) -> dict:
    """The measured facts of the FEA mesh in `workspace`, {} when it holds no FEA mesh."""
    ws = Path(workspace)
    q = _read(ws / "quality.json")
    fea = _m(q.get("fea"))
    if not fea:
        return {}
    plan = _read(ws / "fea_plan.json")
    deck = _m(q.get("deck"))
    exports = _m(fea.get("exports"))
    nastran, ansys = _m(exports.get("nastran")), _m(exports.get("ansys"))
    cells = int(q.get("cells") or 0)
    cad_faces = len(plan.get("faces") or []) or None
    inp_faces = len(deck.get("face_sets") or {}) or None
    per_file = [
        {"solver": "CalculiX / Abaqus", "file": "mesh.inp", "element": "C3D10",
         "present": (ws / "mesh.inp").exists(), "elements": cells or None, "named_faces": inp_faces},
        {"solver": "Nastran", "file": "mesh.bdf", "element": "CTETRA (10-node)",
         "present": (ws / "mesh.bdf").exists(), "elements": nastran.get("elements"),
         "named_faces": nastran.get("faces")},
        {"solver": "Ansys MAPDL", "file": "mesh.cdb", "element": "SOLID187",
         "present": (ws / "mesh.cdb").exists(), "elements": ansys.get("elements"),
         "named_faces": ansys.get("faces")},
    ]
    aside = _m(q.get("fea_set_aside"))
    spots_short = _m(_m(fea.get("rules")).get("hot_spots")).get("short") or []
    return {
        "valid": measured_valid(q),
        "fatal": list(q.get("fatal") or []),
        "inverted": q.get("fea_inverted"),
        "element_order": q.get("element_order"),
        "elements": cells,
        "nodes": q.get("nodes"),
        "cad_faces": cad_faces,
        "files": per_file,
        "through_wall_min": fea.get("through_wall_min"),
        "hot_spots": len(plan.get("hot_spots") or []),
        "hot_spots_short": len(spots_short),
        "coarsened": bool(fea.get("coarsened")),
        "resolution_short": list(fea.get("resolution_short") or []),
        "limit": fea.get("limit"),
        "set_aside": {"elements": aside.get("elements"), "cusp": aside.get("cusp_elements"),
                      "micro": aside.get("micro_elements")},
        "straightened": q.get("fea_straightened"),
        **({"assembly": assembly_checklist(fea.get("assembly"), plan, inp_faces)} if fea.get("assembly") else {}),
    }


def assembly_checklist(asm: Mapping | None, plan: Mapping, named_faces: int | None) -> dict:
    """THE REVIEWER'S CHECKLIST FOR AN ASSEMBLY (PLAN-fea-16), measured from the records: every part
    present (each has elements), every interface shared (bonded: one joint per touching pair) or
    paired (contact / tied: one pair per touching or overlapping pair), every face named."""
    a = _m(asm)
    plan_asm = _m(plan.get("assembly"))
    parts = list(a.get("parts") or [])
    want_parts = len(plan_asm.get("parts") or []) or len(parts)
    # a part wholly inside another is in the mesh as that part's material (bonded): present
    present = sum(1 for p in parts if int(_m(p).get("elements") or 0) > 0) + len(a.get("absorbed") or [])
    joints = list(a.get("joints") or [])
    wanted_joints = plan_asm.get("pairs")
    want_faces = len(plan.get("faces") or [])
    return {
        "mode": a.get("mode"),
        "parts": {"present": present, "expected": want_parts, "ok": present == want_parts and want_parts > 0},
        "joints": {"written": len(joints), "expected": wanted_joints,
                   "ok": wanted_joints is None or len(joints) == int(wanted_joints)},
        "faces": {"named": named_faces, "expected": want_faces or None,
                  "ok": bool(named_faces) and (not want_faces or int(named_faces or 0) >= want_faces)},
    }


def _n(v) -> str:
    return "not measured" if v is None else (f"{v:,}" if isinstance(v, int) else str(v))


def facts_text(workspace) -> str:
    """The facts in plain lines for the reviewer's brief; '' for a workspace with no FEA mesh."""
    f = facts(workspace)
    if not f:
        return ""
    lines = ["MEASURED FEA FACTS (taken by the pipeline from the delivered files - judge against these, "
             "never re-derive them from a picture):",
             "- Validity (the hard pass/fail, measured): "
             + ("VALID - no inverted element, nothing fatal." if f["valid"] is True else
                "NOT VALID - " + ("; ".join(f["fatal"]) or f"{_n(f['inverted'])} inverted") if f["valid"] is False
                else "not measured."),
             f"- Elements: {_n(f['elements'])} quadratic tetrahedra (element order {_n(f['element_order'])}), "
             f"{_n(f['nodes'])} nodes.",
             f"- CAD faces in the part: {_n(f['cad_faces'])}."]
    for row in f["files"]:
        lines.append(f"- {row['file']} ({row['solver']}, {row['element']}): "
                     + ("written" if row["present"] else "MISSING")
                     + f", {_n(row['elements'])} elements, {_n(row['named_faces'])} named faces (Face_k).")
    lines.append(f"- Fewest elements through a wall: {_n(f['through_wall_min'])}.")
    lines.append(f"- Hot spots refined: {_n(f['hot_spots'])}; short of their rule: {_n(f['hot_spots_short'])}.")
    if f["coarsened"]:
        lines.append(f"- The plan was coarsened to stay under {_n(f['limit'])} elements; short of a rule: "
                     + (", ".join(str(s) for s in f["resolution_short"][:8]) or "nothing listed") + ".")
    sa = f["set_aside"]
    if sa.get("elements"):
        lines.append(f"- Set aside from the quality floor: {_n(sa.get('elements'))} elements "
                     f"({_n(sa.get('cusp'))} at a cusp in the CAD, {_n(sa.get('micro'))} at a micro detail).")
    asm = f.get("assembly")
    if asm:
        joined = {"bonded": "shared nodes (one conformal mesh)", "contact": "named contact pairs",
                  "tie": "tied surface pairs"}.get(str(asm.get("mode")), str(asm.get("mode")))
        lines.append(f"- ASSEMBLY ({joined}). Every part present: "
                     + ("yes" if asm["parts"]["ok"] else "NO")
                     + f" ({_n(asm['parts']['present'])} of {_n(asm['parts']['expected'])} parts have elements). "
                     + "Every interface shared or paired: " + ("yes" if asm["joints"]["ok"] else "NO")
                     + f" ({_n(asm['joints']['written'])} written of {_n(asm['joints']['expected'])}). "
                     + "Every face named (Part_k_Face_j): " + ("yes" if asm["faces"]["ok"] else "NO")
                     + f" ({_n(asm['faces']['named'])} of {_n(asm['faces']['expected'])}).")
    lines.append("- " + ONE_PREVIEW_GROUP)
    return "\n".join(lines)


__all__ = ["FEA_PURPOSE", "assembly_checklist", "ONE_PREVIEW_GROUP", "SOLVER_FILES", "facts", "facts_text", "is_fea", "measured_valid"]
