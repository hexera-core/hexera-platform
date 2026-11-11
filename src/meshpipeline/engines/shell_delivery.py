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


def user_limits(quality: Mapping) -> list[Mapping]:
    """SHMESH-26: the quality limits the user's notes set that were measured on this shell (the requirement rows of
    a limit, applied); [] when the notes set none."""
    rows = _map(quality.get("requirements")).get("rows") or []
    return [r for r in rows if isinstance(r, Mapping) and str(r.get("field") or "").startswith("limit:")
            and r.get("applied") is not False]


def limits(quality: Mapping) -> list[dict]:
    """The shell's verdict rows (VER-2 for a shell): every figure of engines/shell_quality.LIMITS at its worst against
    its limit, and the elements past Nastran's own GEOMCHECK limits. Notes, never blockers: validity blocks.
    SHMESH-26: WHILE THE USER'S OWN LIMITS ARE IN FORCE the shell is judged by them alone - our usual targets are no
    rows at all (they are a quiet "also measured" line, `also_measured`); Nastran's own limits still are."""
    from meshpipeline.engines.shell_quality import LIMITS

    sq = _map(quality.get("shell_quality"))
    rows = []
    for key, op, limit, source in (() if user_limits(quality) else LIMITS):
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


def also_measured(quality: Mapping) -> str:
    """While the user's limits are in force: our usual shell targets a figure is past, in one quiet line - never a
    failure, never "past a limit" ("" when none is, or when the user set no limit)."""
    from meshpipeline.engines.shell_quality import LIMITS

    if not user_limits(quality):
        return ""
    sq = _map(quality.get("shell_quality"))
    said = []
    for key, op, limit, _source in LIMITS:
        v = _worst(sq, key)
        if v is None or (v >= limit if op == ">=" else v <= limit):
            continue
        said.append(f"{LABELS.get(key, key).split(' (')[0].lower()} {v:.4g} (our target {'at least' if op == '>=' else 'at most'} "
                    f"{limit:g})")
    return ("Also measured against our usual shell targets, not judged while your own limits are in force: "
            + "; ".join(said) + ".") if said else ""


def header_row(quality: Mapping) -> dict | None:
    """The result header's worst-quality figure while the user set a Jacobian limit: the worst element by their
    Jacobian against their outer limit (their target when they gave none), marked as their own line is - met or
    not - so the header never says "needs 0.6+" in amber over an element their 1% band allows. None otherwise."""
    for r in user_limits(quality):
        if r.get("field") != "limit:jacobian" or r.get("worst") is None:
            continue
        bound = r.get("limit_outer") if r.get("limit_outer") is not None else r.get("limit_target")
        return {"key": WORST_KEY, "label": f"Worst element (Jacobian, as you asked: {r.get('asked')})", "op": ">=",
                "limit": bound, "measured": r.get("worst"), "ok": r.get("met"), "gating": False, "asked": True,
                "words": user_line(r), "row_id": r.get("id")}
    return None


def asked_rows(quality: Mapping) -> list[dict]:
    """SHMESH-26: THE USER'S OWN REQUIREMENTS (read from their notes) as rows of the same list
    (engines/mesh_requirements.asked_rows): measured as the elements outside what was asked, never a blocker."""
    from meshpipeline.engines.mesh_requirements import asked_rows as rows

    return rows(quality, source="your notes (engines/shell_requirements.py)")


def user_line(row: Mapping) -> str:
    """One requirement of the user's as the result says it: asked, then delivered."""
    from meshpipeline.engines.mesh_requirements import line

    return line(row)


def quality_of(rec: Mapping) -> dict:
    """quality.json for a delivered shell, from the shell run's record."""
    sq = dict(rec.get("quality") or {})
    invalid = int(sq.get("invalid") or 0)
    faces = list(rec.get("faces") or [])
    return {
        "cells": int(rec.get("elements") or 0), "nodes": int(rec.get("nodes") or 0), "element_order": 1,
        "fatal": [f"{invalid} elements are folded or flat"] if invalid else [],
        "bounds": rec.get("bounds_m") or [0, 0, 0, 1, 1, 1],
        "groups": {"free": "free"}, "default_group": "free", "default_group_used": True,
        "quality_bar": "fea", "min_sicn": None, "fea_inverted": invalid,
        "shell_quality": sq,
        **({"requirements": rec["requirements"]} if rec.get("requirements") else {}),
        "fea": {"plan": True, "shell": True, "walls_mm": list(rec.get("walls_mm") or []),
                # SURF-3: a shell made on a surface file, and whether the user gave its thickness
                **({"surface": True, "thickness_set": bool(_map(rec.get("surface")).get("thickness_set"))}
                   if _map(rec.get("surface")).get("kind") == "surface" else {}),
                "face_tags": {str(f["name"]): [int(f["tag"])] for f in faces if f.get("tag")}},
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
    (ws / "quality.json").write_text(json.dumps(quality_of(rec), indent=1, sort_keys=True, default=float))
    tgt = dict(rec.get("target") or {})
    from meshpipeline.engines.shell_plan import count_words

    plan: dict[str, Any] = {"shell": True, "words": [w for w in (rec.get("words"), count_words(tgt)) if w],
            "target": tgt or None, "faces": [], "hot_spots": [], "asked": dict(target or {}) or None}
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
    # SHMESH-26: the header's worst-quality figure is the user's Jacobian line when they set one (in place of its row,
    # so it is listed once)
    head = header_row(q)
    if head is not None:
        rows = [r for r in rows if r["key"] != f"asked_{head['row_id']}"]
        rows.insert(len(gating), {k: v for k, v in head.items() if k != "row_id"})
    mine = bool(user_limits(q))
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
        if mine:
            # judged by the user's own limits alone: said so, and the edge-length targets with their share
            said = "Passed: every limit your notes set is met, and every element is inside Nastran's own checks."
            shares = [f"{r.get('outside_pct'):g}% of elements have an edge "
                      f"{'shorter' if r.get('field') == 'min_edge' else 'longer'} than you asked"
                      for r in asked if r.get("target") and r.get("met") is None and r.get("outside")]
            if shares:
                said += " Edge-length targets: " + "; ".join(shares) + "."
        elif asked and all(r.get("met") is True for r in asked):
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
    within = bool(sq.get("within_nastran"))
    solvers = []
    for key, label, file, element, tested in SHELL_SOLVERS:
        ok = within if key == "nastran" else not q.get("fatal")
        solvers.append({"solver": key, "label": label, "file": file, "element": element, "tested": bool(tested),
                        "tested_words": "solved in CalculiX in our tests" if tested else "not yet tested in the real solver",
                        "outcome": "pass" if ok else "warnings",
                        "words": f"{label}: {element} plates" + ("" if ok else " (some elements past its own checks)") + ".",
                        "rows": [], "file_ready": present(ws, file)})
    q_n, t_n = int(sq.get("quads") or rec.get("quads") or 0), int(sq.get("trias") or rec.get("trias") or 0)
    walls = list((q.get("fea") or {}).get("walls_mm") or [])
    plan_words = ([w for w in [rec.get("words")] if w] + [user_line(r) for r in asked]
                  + [str(w) for w in plan.get("not_applied") or []] + [w for w in [also_measured(q)] if w])
    summary = SUMMARY.format(n=cells, q=q_n, t=t_n)
    if _map(rec.get("surface")).get("kind") == "surface":
        # SURF-6: said as what it is - on the user's surface, at their thickness (or none set)
        from meshpipeline.contracts.shell_thickness import words as mm_words

        t_mm = _map(rec.get("surface")).get("thickness_mm")
        summary = SURFACE_SUMMARY.format(n=cells, q=q_n, t=t_n, thick=f"{mm_words(t_mm)} thick" if t_mm
                                         else "thickness not set")
    return {**extra,
            "title": "FEA shell mesh",
            "shell": {"walls_mm": walls, "quads": q_n, "trias": t_n},
            "summary": summary,
            "level": "Shell",
            "units": {"mesh": COMPLETED_MESH_UNIT.value, "words": export_units_words()},
            "check": {"outcome": outcome, "words": said, "rows": rows,
                      "solvers": ("Limits: your own, from your notes, and Nastran's own GEOMCHECK defaults." if mine
                                  else "Limits: the shell industry targets, and Nastran's own GEOMCHECK defaults.")},
            "elements": cells,
            "through_wall": {},
            "plan_words": plan_words,
            "hot_spots": [],
            "faces": [],
            "download": {"words": "The full download holds:",
                         "contains": [{"file": f, "words": w} for f, w in CONTAINS if present(ws, f)],
                         "later": "Every shell file has the same node and element numbers, and its lengths in metres."},
            "solvers": solvers}


__all__ = ["CONTAINS", "LABELS", "MARKER_SOURCE", "SUMMARY", "SURFACE_SUMMARY", "WORST_KEY", "also_measured",
           "asked_rows", "header_row", "user_limits", "deliver", "is_shell",
           "is_shell_workspace",
           "limits", "quality_of", "results"]
