#!/usr/bin/env python3
# Responsibility: Mesh a RETURNED STEP the two extra ways the meshability report needs, each in a
#                 bounded fresh interpreter: with gmsh's own defaults (what a customer gets out of
#                 the box) and with the mesh-size hints sidecar the repair wrote beside the STEP.
# Owns: the hints sidecar's name and schema check, the two gmsh settings, the child protocol.
# Boundaries: EVAL ONLY (devtools). The headline "default settings" verdict is NOT made here: it is
#             cad/repair/mesh_probe.probe_file (MeshSizeMax = diagonal / 40), the probe every wave
#             since wave 1 was scored on. This module never raises and never blocks past its budget.
"""Extra gmsh verdicts on a returned STEP.

    probe(step, diagonal_mm=..., mode="gmsh_default")             # no size setting at all
    probe(step, diagonal_mm=..., mode="hints", hints=load_hints(p)) # diagonal/40 plus face sizes

THE HINTS SIDECAR is `<returned STEP>.mesh-hints.json`:

    {"schema": "crs-mesh-hints/1", "units": "mm", "global_max_mm": null | float,
     "faces": [{"face": <1-based OCC face index in the RETURNED STEP>, "fingerprint": "...",
                "size_mm": float, "why": "..."}]}

A face index is used as the gmsh surface tag only when gmsh imported exactly as many surfaces as
the hints were written against (`face_count`, when given) and every hinted index exists; otherwise
the hints are reported `unmappable` and not applied. The schema is provisional until lane G
publishes the final one (routed through LEAD).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HINTS_SUFFIX = ".mesh-hints.json"
SCHEMA = "crs-mesh-hints/1"
#: The headline probe's size rule, repeated here only so the hints mode starts from the same base.
SIZE_DIVISOR = 40.0
MODES = ("gmsh_default", "hints")


def hints_path(step: Path) -> Path:
    return Path(f"{step}{HINTS_SUFFIX}")


def load_hints(path: Path) -> dict | None:
    """The sidecar as a dict when it exists and has the expected schema, else None."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    faces = data.get("faces")
    if not isinstance(faces, list):
        return None
    clean = []
    for f in faces:
        try:
            clean.append({"face": int(f["face"]), "size_mm": float(f["size_mm"])})
        except (KeyError, TypeError, ValueError):
            return None
        if clean[-1]["face"] < 1 or clean[-1]["size_mm"] <= 0:
            return None
    out = {"faces": clean, "global_max_mm": data.get("global_max_mm")}
    if data.get("face_count") is not None:
        out["face_count"] = int(data["face_count"])
    return out


def _child(step: str, diagonal_mm: float, mode: str, hints: dict | None) -> dict:
    """Runs in the child: one gmsh attempt, the answer as a dict."""
    import gmsh

    gmsh.initialize()
    stage = "import"
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.logger.start()
        gmsh.model.occ.importShapes(step)
        gmsh.model.occ.synchronize()
        if mode == "hints":
            gmax = hints.get("global_max_mm") if hints else None
            gmsh.option.setNumber("Mesh.MeshSizeMax",
                                  float(gmax) if gmax else diagonal_mm / SIZE_DIVISOR)
            surfaces = {int(t) for _d, t in gmsh.model.getEntities(2)}
            want = (hints or {}).get("faces") or []
            expected = (hints or {}).get("face_count")
            if (expected is not None and expected != len(surfaces)) or \
                    any(f["face"] not in surfaces for f in want):
                return {"meshed": False, "kind": "unmappable", "stage": stage, "applied": 0}
            fields = []
            for f in want:
                fid = gmsh.model.mesh.field.add("Constant")
                gmsh.model.mesh.field.setNumber(fid, "VIn", f["size_mm"])
                gmsh.model.mesh.field.setNumber(fid, "VOut", 1e22)
                gmsh.model.mesh.field.setNumbers(fid, "SurfacesList", [f["face"]])
                fields.append(fid)
            if fields:
                fmin = gmsh.model.mesh.field.add("Min")
                gmsh.model.mesh.field.setNumbers(fmin, "FieldsList", fields)
                gmsh.model.mesh.field.setAsBackgroundMesh(fmin)
        stage = "2d"
        gmsh.model.mesh.generate(2)
        stage = "3d"
        gmsh.model.mesh.generate(3)
        _types, tags, _nodes = gmsh.model.mesh.getElements(3)
        count = sum(len(t) for t in tags)
        return {"meshed": count > 0, "kind": "" if count > 0 else "no_volume", "stage": stage,
                "elements": count, "applied": len((hints or {}).get("faces") or [])}
    except Exception as exc:  # noqa: BLE001 - a gmsh failure is the answer
        kind = "other"
        try:
            from meshpipeline.cad.repair.mesh_probe import classify
            kind = str(classify(str(exc), gmsh.logger.get(), stage=stage).kind)
        except Exception:  # noqa: BLE001
            pass
        return {"meshed": False, "kind": kind, "stage": stage, "message": str(exc)[:300]}
    finally:
        try:
            gmsh.finalize()
        except Exception:  # noqa: BLE001
            pass


def probe(step: Path, *, diagonal_mm: float, mode: str, hints: dict | None = None,
          budget_s: float = 120.0) -> dict:
    """{"meshed": bool, "kind": str, ...}. NEVER raises; a child past `budget_s` is `timeout`."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    payload = json.dumps({"step": str(step), "diagonal_mm": float(diagonal_mm), "mode": mode,
                          "hints": hints})
    try:
        proc = subprocess.run([sys.executable, "-W", "ignore", __file__, "--child"],
                              input=payload, capture_output=True, text=True,
                              timeout=max(budget_s, 0.0), start_new_session=True)
    except subprocess.TimeoutExpired:
        return {"meshed": False, "kind": "timeout"}
    except OSError as exc:
        return {"meshed": False, "kind": "probe_error", "message": str(exc)[:200]}
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        answer = json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"meshed": False, "kind": "probe_error"}
    return answer if isinstance(answer, dict) else {"meshed": False, "kind": "probe_error"}


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        job = json.loads(sys.stdin.read())
        sys.stdout.write("\n" + json.dumps(_child(job["step"], job["diagonal_mm"], job["mode"],
                                                  job.get("hints"))) + "\n")
        sys.exit(0)
    sys.exit("usage: imported by repair_corpus; the child is spawned with --child")
