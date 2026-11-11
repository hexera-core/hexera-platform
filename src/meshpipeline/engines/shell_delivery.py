# Responsibility: A SHELL RUN DELIVERS THE SHELL (SHSTAGE-7). When the user asked for a shell and engines/shell_run made
# it, the shell IS the result: no solid tet mesh is made beside it. This writes the records the delivery path reads
# (the deck marker mesh.inp, the viewer's mesh.msh, quality.json, fea_plan.json, gmsh_spec.json, fea_passes.json) from
# the shell run's own record, and builds what the result screen and the verdict say of it: the shell's element count,
# its quality against the shell limits (engines/shell_quality), the user's count, and one plain line.
# Boundaries: deterministic, reads and writes only the workspace; never meshes. A shell run that made no shell is
# not delivered here (engines/fea_driver meshes the solid instead, said).
# Collaborates with: engines/shell_run.py (the shell, its record and shell.msh), engines/fea_driver.py (calls
# deliver), engines/fea_results.py and engines/verdict_limits.py (the result block and the rows), engines/gmsh/
# gmsh_runner.py and gates.py (validity), application/artifact_uploader.py (the downloads).
from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: the shell deck that stands as the delivery's marker (mesh.inp): CalculiX S4 / S3, the file solved in our tests
MARKER_SOURCE = "shell.inp"
#: (figure key in shell_quality.LIMITS, the row's plain label)
LABELS = {
    "jacobian_ratio": "Worst element shape (Jacobian ratio, 1 is perfect)",
    "aspect": "Longest side over shortest side",
    "warp_deg": "Warp out of plane (degrees)",
    "skew_deg": "Skew (degrees)",
    "taper": "Taper",
    "min_angle_quad": "Smallest corner angle, quads (degrees)",
    "max_angle_quad": "Largest corner angle, quads (degrees)",
    "min_angle_tria": "Smallest corner angle, triangles (degrees)",
    "max_angle_tria": "Largest corner angle, triangles (degrees)",
}
#: the key the result screen reads as "Worst quality"
WORST_KEY = "shell_jacobian_ratio"
#: the one plain line every shell result says
SUMMARY = ("A mid-surface shell: {n:,} plate elements ({q:,} quads, {t:,} triangles); each element carries its wall "
           "thickness in the file.")
#: SURF-6: the same line for a shell made on a surface file (engines/surface_run): what it lies on, and its thickness
SURFACE_SUMMARY = "A shell on your surface: {n:,} plate elements ({q:,} quads, {t:,} triangles), {thick}."
#: STD-4: the same lines for an all-triangle shell (Triangles picked on the stage, or asked in the notes or settings)
TRIA_SUMMARY = ("A mid-surface shell: {n:,} triangular plate elements (3-node triangles); each element carries its wall "
                "thickness in the file.")
TRIA_SURFACE_SUMMARY = "A shell on your surface: {n:,} triangular plate elements (3-node triangles), {thick}."
CONTAINS = (
    ("shell.bdf", "Nastran bulk data: CQUAD4 / CTRIA3, a PSHELL per wall thickness"),
    ("shell.inp", "CalculiX input deck: S4 / S3, a *SHELL SECTION per wall thickness"),
    ("shell_abaqus.inp", "Abaqus input deck: S4R / S3, a *SHELL SECTION per wall thickness"),
    ("shell.cdb", "Ansys MAPDL archive: SHELL181, a section per wall thickness"),
    ("shell_run.json", "how the shell was made: the mid-surface, the walls and the sizes"),
)


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _map(v) -> Mapping:
    return v if isinstance(v, Mapping) else {}


def is_shell(quality: Mapping | None) -> bool:
    """Whether a quality record is a delivered shell's."""
    fea = (quality or {}).get("fea")
    return isinstance(fea, Mapping) and bool(fea.get("shell"))


def is_shell_workspace(workspace) -> bool:
    return is_shell(_read(Path(workspace) / "quality.json"))


def _worst(sq: Mapping, key: str):
    v = ((sq.get("figures") or {}).get(key) or {}).get("worst")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def limits(quality: Mapping) -> list[dict]:
    """The shell's verdict rows (VER-2 for a shell): every figure of engines/shell_quality.LIMITS at its worst against
    its limit, and the elements past Nastran's own GEOMCHECK limits. Notes, never blockers: validity blocks."""
    from meshpipeline.engines.shell_quality import LIMITS

    sq = _map(quality.get("shell_quality"))
    rows = []
    for key, op, limit, source in LIMITS:
        measured = _worst(sq, key)
        if measured is None:
            continue
        rows.append({"key": f"shell_{key}", "label": LABELS.get(key, key), "op": op, "limit": limit,
                     "measured": measured, "source": f"engines/shell_quality.py ({source})"})
    past = _map(sq.get("nastran_past"))
    rows.append({"key": "shell_nastran_past", "label": "Elements past Nastran's own GEOMCHECK limits", "op": "==",
                 "limit": 0, "measured": sum([int(v) for v in past.values()]),
                 "source": "engines/shell_quality.py (MSC Nastran GEOMCHECK defaults)"})
    rows.extend(asked_rows(quality))
    return rows


def asked_rows(quality: Mapping) -> list[dict]:
    """SHMESH-26: THE USER'S OWN REQUIREMENTS (read from their notes) as rows of the same list
    (engines/mesh_requirements.asked_rows): measured as the elements outside what was asked, never a blocker."""
    from meshpipeline.engines.mesh_requirements import asked_rows as rows

    return rows(quality, source="your notes (engines/shell_requirements.py)")


def user_line(row: Mapping) -> str:
    """One requirement of the user's as the result says it: asked, then delivered."""
    from meshpipeline.engines.mesh_requirements import line

    return line(row)


def part_names(workspace) -> dict[str, str]:
    """Each body's name as the user knows it: the CAD's own (the STEP product names, read when the assembly was
    staged: cad/fea_assembly's assembly.json), else Part_k. Empty when the workspace says nothing of its bodies."""
    from meshpipeline.cad.fea_assembly import SIDECAR

    parts = _read(Path(workspace) / SIDECAR).get("parts") or []
    return {str(p["id"]): str(p.get("name") or "").strip() or str(p["id"])
            for p in parts if isinstance(p, Mapping) and p.get("id")}


def part_tags(workspace, rec: Mapping) -> dict[str, list[int]]:
    """THE SHELL'S PARTS FOR THE VIEWER: each body by its name (part_names), holding the shell.msh surfaces of its
    mid-faces (cad/midsurface's record, shell.json: each mid-face's body). Empty when a mid-face cannot be traced
    to its body - the viewer then lists the mid-faces themselves, as before."""
    mids = _read(Path(workspace) / "shell.json").get("mids") or []
    body_of = {str(m.get("id")): str(m.get("body") or "") for m in mids if isinstance(m, Mapping)}
    names = part_names(workspace)
    out: dict[str, list[int]] = {}
    for f in rec.get("faces") or []:
        body = body_of.get(str(f.get("name")))
        if not body or not f.get("tag"):
            return {}
        out.setdefault(names.get(body, body), []).append(int(f["tag"]))
    return out


def quality_of(rec: Mapping, parts: Mapping | None = None) -> dict:
    """quality.json for a delivered shell, from the shell run's record. `parts`: part_tags."""
    sq = dict(rec.get("quality") or {})
    invalid = int(sq.get("invalid") or 0)
    faces = list(rec.get("faces") or [])
    return {
        "cells": int(rec.get("elements") or 0), "nodes": int(rec.get("nodes") or 0), "element_order": 1,
        "fatal": [f"{invalid} elements are folded or flat"] if invalid else [],
        "bounds": rec.get("bounds_m") or [0, 0, 0, 1, 1, 1],
        # NO "free" GROUP: every element of a shell is in its mid-face's group, so a default group would be an
        # empty row in the viewer's parts list; the parts are the drawn surfaces themselves (part_tags)
        "groups": {}, "default_group": "free", "default_group_used": False,
        "quality_bar": "fea", "min_sicn": None, "fea_inverted": invalid,
        "shell_quality": sq,
        **({"requirements": rec["requirements"]} if rec.get("requirements") else {}),
        "fea": {"plan": True, "shell": True, "walls_mm": list(rec.get("walls_mm") or []),
                # SURF-3: a shell made on a surface file, and whether the user gave its thickness
                **({"surface": True, "thickness_set": bool(_map(rec.get("surface")).get("thickness_set"))}
                   if _map(rec.get("surface")).get("kind") == "surface" else {}),
                "face_tags": {str(f["name"]): [int(f["tag"])] for f in faces if f.get("tag")},
                **({"part_tags": {str(k): [int(t) for t in v] for k, v in parts.items()}} if parts else {})},
    }


def deliver(workspace, rec: Mapping, *, target: Mapping | None = None) -> bool:
    """Write the delivery's records for the shell the run made. False (nothing written) when it made none."""
    from meshpipeline.engines import shell_run

    ws = Path(workspace)
    if not rec.get("made") or not (ws / MARKER_SOURCE).exists() or not (ws / shell_run.MSH).exists():
        return False
    shutil.copyfile(ws / MARKER_SOURCE, ws / "mesh.inp")
    shutil.copyfile(ws / shell_run.MSH, ws / "mesh.msh")
    for stale in ("mesh.bdf", "mesh.cdb"):
        (ws / stale).unlink(missing_ok=True)
    (ws / "quality.json").write_text(json.dumps(quality_of(rec, part_tags(ws, rec)), indent=1, sort_keys=True,
                                                default=float))
    tgt = dict(rec.get("target") or {})
    from meshpipeline.engines.shell_plan import count_words

    plan: dict[str, Any] = {"shell": True, "words": [w for w in (rec.get("words"), count_words(tgt)) if w],
            "target": tgt or None, "faces": [], "hot_spots": [], "asked": dict(target or {}) or None}
    # STD-4 / SHSTAGE-8: which elements were made and why ("Triangles, as chosen on the stage."), said first
    from meshpipeline.engines import mesh_requirements as _MR
    from meshpipeline.engines.meshing_standard import elements_of

    plan["elements"] = elements_of(_MR.read(ws))
    plan["words"] = [plan["elements"]["words"], *plan["words"]]
    if rec.get("requirements"):
        # SHMESH-26: what the user's notes asked for, against what this mesh delivers
        from meshpipeline.engines.shell_requirements import words as asked_words

        plan["requirements"] = rec["requirements"]
        plan["words"] = [*list(plan["words"] or []), *asked_words(rec["requirements"])]
    from meshpipeline.engines import mesh_requirements as MR

    told = MR.not_applied_words(MR.read(ws))
    if told:
        # NOTE-8: what the notes say about the mesh that was not applied stays with the result
        plan["not_applied"] = told
        plan["words"] = [*list(plan["words"] or []), *told]
    (ws / "fea_plan.json").write_text(json.dumps(plan, indent=1, sort_keys=True, default=float))
    (ws / "gmsh_spec.json").write_text(json.dumps({"element_order": 1, "shell": True,
                                                   "made_by": "engines/shell_run.py"}, indent=1))
    (ws / "fea_passes.json").write_text(json.dumps({"passes": [], "delivered": "shell", "levers": []}, indent=1))
    return True


def _element_words(element: str, tria: bool) -> str:
    """A solver's shell element as the result names it: "CQUAD4 / CTRIA3", or for an all-triangle shell its triangle
    only ("CTRIA3", "S3", "SHELL181 triangle")."""
    if not tria:
        return element
    return {"S4 / S3": "S3", "S4R / S3": "S3", "CQUAD4 / CTRIA3": "CTRIA3", "SHELL181": "SHELL181 triangle"}.get(
        element, element)


def results(workspace, q: Mapping, plan: Mapping) -> dict:
    """The result block for a delivered shell, in the shape engines/fea_results.results gives a solid."""
    from meshpipeline.contracts.decision_record import limit_met
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT, export_units_words
    from meshpipeline.engines.bucket_files import present
    from meshpipeline.engines.shell_plan import count_words
    from meshpipeline.engines.shell_writers import SHELL_SOLVERS
    from meshpipeline.engines.verdict_limits import words

    ws = Path(workspace)
    rec = _read(ws / "shell_run.json")
    sq = _map(q.get("shell_quality"))
    cells = int(q.get("cells") or 0)
    gating = [{"key": "fea_inverted", "label": "Folded or flat elements (a corner Jacobian not above zero)",
               "op": "==", "limit": 0, "measured": q.get("fea_inverted")}]
    rows = []
    for r in gating + limits(q):
        met = limit_met(r.get("measured"), str(r.get("op")), r.get("limit"))
        rows.append({**{k: r.get(k) for k in ("key", "label", "op", "limit", "measured")}, "ok": met,
                     "gating": r in gating, "words": words(r)})
    asked = [r for r in _map(q.get("requirements")).get("rows") or [] if isinstance(r, Mapping)]
    missed = sum(1 for r in asked if r.get("met") is False)
    if any(r["ok"] is False and r["gating"] for r in rows) or q.get("fatal"):
        outcome, said = "fail", "The mesh fails a check no solver can work around."
    elif any(r["ok"] is False for r in rows):
        outcome, said = "concerns", "Passed, with notes: the shell is valid; some figures are past a limit (listed)."
        if missed:
            # SHMESH-26: delivered, and told plainly - never a failure, never "used"
            judged = sum(1 for r in asked if r.get("met") in (True, False))
            said = (f"Delivered, with notes: the shell is valid; {missed} of the {judged} limits your notes set "
                    "could not be fully met (each is listed with how far off it is).")
    else:
        outcome, said = "pass", "Every element meets the shell limits and Nastran's own checks."
        if asked and all(r.get("met") is True for r in asked):
            said += " Everything your notes asked for is met."
    extra: dict[str, Any] = {}
    if asked:
        extra["requirements"] = {"rows": asked, "missed": missed,
                                 "definitions": _map(q.get("requirements")).get("definitions")}
    tgt = plan.get("target") or {}
    if tgt.get("asked"):
        line = count_words(tgt)
        extra["target"] = {"asked": int(tgt["asked"]), "mode": tgt.get("mode") or "about",
                           "planned": int(tgt.get("planned") or 0), "built": cells, "words": line}
    q_n, t_n = int(sq.get("quads") or rec.get("quads") or 0), int(sq.get("trias") or rec.get("trias") or 0)
    # STD-4: an all-triangle shell says so (no "0 quads")
    tria = bool(cells) and q_n == 0 and t_n > 0
    within = bool(sq.get("within_nastran"))
    solvers = []
    for key, label, file, element, tested in SHELL_SOLVERS:
        ok = within if key == "nastran" else not q.get("fatal")
        solvers.append({"solver": key, "label": label, "file": file, "element": element, "tested": bool(tested),
                        "tested_words": "solved in CalculiX in our tests" if tested else "not yet tested in the real solver",
                        "outcome": "pass" if ok else "warnings",
                        "words": f"{label}: {_element_words(element, tria)} plates"
                                 + ("" if ok else " (some elements past its own checks)") + ".",
                        "rows": [], "file_ready": present(ws, file)})
    walls = list((q.get("fea") or {}).get("walls_mm") or [])
    elements = _map(plan.get("elements"))
    plan_words = ([w for w in [elements.get("words"), rec.get("words")] if w] + [user_line(r) for r in asked]
                  + [str(w) for w in plan.get("not_applied") or []])
    summary = (TRIA_SUMMARY if tria else SUMMARY).format(n=cells, q=q_n, t=t_n)
    if _map(rec.get("surface")).get("kind") == "surface":
        # SURF-6: said as what it is - on the user's surface, at their thickness (or none set)
        from meshpipeline.contracts.shell_thickness import words as mm_words

        t_mm = _map(rec.get("surface")).get("thickness_mm")
        summary = (TRIA_SURFACE_SUMMARY if tria else SURFACE_SUMMARY).format(
            n=cells, q=q_n, t=t_n, thick=f"{mm_words(t_mm)} thick" if t_mm else "thickness not set")
    return {**extra,
            "title": "FEA shell mesh",
            "shell": {"walls_mm": walls, "quads": q_n, "trias": t_n,
                      **({"elements": dict(elements)} if elements else {})},
            "summary": summary,
            "level": "Shell",
            "units": {"mesh": COMPLETED_MESH_UNIT.value, "words": export_units_words()},
            "check": {"outcome": outcome, "words": said, "rows": rows,
                      "solvers": "Limits: the shell industry targets, and Nastran's own GEOMCHECK defaults."},
            "elements": cells,
            "through_wall": {},
            "plan_words": plan_words,
            "hot_spots": [],
            "faces": [],
            "download": {"words": "The full download holds:",
                         "contains": [{"file": f, "words": w} for f, w in CONTAINS if present(ws, f)],
                         "later": "Every shell file has the same node and element numbers, and its lengths in metres."},
            "solvers": solvers}


__all__ = ["CONTAINS", "LABELS", "MARKER_SOURCE", "SUMMARY", "SURFACE_SUMMARY", "WORST_KEY", "asked_rows", "deliver", "is_shell",
           "part_names", "part_tags",
           "is_shell_workspace",
           "limits", "quality_of", "results"]
