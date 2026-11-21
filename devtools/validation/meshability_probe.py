#!/usr/bin/env python3
# Responsibility: Mesh a STEP with gmsh's OWN DEFAULTS (no size setting at all: what a customer
#                 gets out of the box), in a bounded fresh interpreter, with the mesh's quality.
# Owns: the gmsh-defaults setting and the child protocol. NOT hints: reading and applying a
#       crs-mesh-hints/1 sidecar is lane G's (cad/repair/mesh_hints), the one implementation.
# Boundaries: EVAL ONLY (devtools). The headline "default settings" verdict is NOT made here: it is
#             cad/repair/mesh_probe.probe_file (MeshSizeMax = diagonal / 40), the probe every wave
#             since wave 1 was scored on. This module never raises and never blocks past its budget.
"""gmsh's own defaults on a STEP.

    probe(step, diagonal_mm=..., mode="gmsh_default")  # no size setting at all

The diagonal is accepted for a uniform call shape and ignored: gmsh's defaults are the point.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MODES = ("gmsh_default",)


def _quality(gmsh, elements: list[int]) -> dict:
    """The volume mesh's quality, so "it meshed" cannot hide a degenerate or enormous mesh: element
    count, min SICN (<= 0 means an inverted or degenerate element), min gamma, and the shortest
    and longest element edge in mm."""
    out: dict = {"elements": len(elements)}
    for name, key, pick in (("minSICN", "min_sicn", min), ("gamma", "min_gamma", min),
                            ("minEdge", "min_edge_mm", min), ("maxEdge", "max_edge_mm", max)):
        try:
            values = gmsh.model.mesh.getElementQualities(elements, name)
            out[key] = round(float(pick(values)), 6) if len(values) else None
        except Exception:  # noqa: BLE001 - a quality gmsh cannot compute is reported missing
            out[key] = None
    return out


def _child(step: str, diagonal_mm: float, mode: str) -> dict:
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
        stage = "2d"
        gmsh.model.mesh.generate(2)
        stage = "3d"
        gmsh.model.mesh.generate(3)
        _types, tags, _nodes = gmsh.model.mesh.getElements(3)
        count = sum(len(t) for t in tags)
        answer: dict = {"meshed": count > 0, "kind": "" if count > 0 else "no_volume",
                        "stage": stage, "elements": count}
        if count > 0:
            answer["quality"] = _quality(gmsh, [int(t) for block in tags for t in block])
        return answer
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


def probe(step: Path, *, diagonal_mm: float, mode: str, budget_s: float = 120.0) -> dict:
    """{"meshed": bool, "kind": str, ...}. NEVER raises; a child past `budget_s` is `timeout`."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    payload = json.dumps({"step": str(step), "diagonal_mm": float(diagonal_mm), "mode": mode})
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
        sys.stdout.write("\n" + json.dumps(_child(job["step"], job["diagonal_mm"],
                                                  job["mode"])) + "\n")
        sys.exit(0)
    sys.exit("usage: imported by repair_corpus; the child is spawned with --child")
