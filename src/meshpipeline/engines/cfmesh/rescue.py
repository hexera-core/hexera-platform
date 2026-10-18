# Responsibility: Give a cfMesh mesh that came out at or past its validity edge the engine's own second chances, inside the run it was built in, and keep whichever mesh measures best.
# Owns: the near-edge trigger, the order of the quality levers, the ranking of candidate meshes, and the rescue record.
# Boundaries: it runs cfMesh's own binaries on the workspace it is handed and measures with checkMesh; it never changes what was asked (cells, layers, patches) and never judges delivery - the gates do.
# Collaborates with: engines/cfmesh/native.py (calls it after the first checkMesh), foam_exec.check_mesh.
"""A CFMESH MESH AT ITS VALIDITY EDGE GETS THE ENGINE'S OWN SECOND CHANCES.

cartesianMesh is threaded and not deterministic. Near the edge of what it can mesh, the same
case comes out differently run to run: volute_scroll_001 gave a worst non-orthogonality of 82.4,
84.3 and 85.6 degrees on three identical runs, and once a face pointing the wrong way, which no
solver accepts (cfm-before / cfm-after / cfm-rerun, 2026-10-18). Its final untangling had left
ONE face. Delivered as it came, the run is a coin toss.

So a mesh that comes out broken (checkMesh's fatal defects) or past NEAR_EDGE_NON_ORTHO is not
handed over as it is. In the time its run cap still holds, this module tries, in order:

  1. OPTIMISE: cfMesh's own improveMeshQuality on the mesh as built - the optimiser cartesianMesh
     runs last, run again with more passes. Same cells, same layers, points moved.
  2. RE-MESH, PROTECTING THE LAYERS: cartesianMesh again with the boundary-layer optimiser asked
     for more smoothing passes and a lower featureSizeFactor (layers may be no thicker than this
     share of the local feature size, so the stacks off two sides of a thin lip or a sharp
     trailing edge no longer meet), then step 1 on that mesh if it is still near the edge. Same
     cell sizes, same layer count; only how the layers are fitted changes.

Every candidate is measured with checkMesh and the BEST MEASURED mesh is kept: one with no fatal
defect beats any with one, then the lowest worst non-orthogonality wins. The mesh as built is a
candidate too, so a lever can only ever improve what is delivered. What was tried and what was
kept is recorded beside the mesh (RESCUE_FILE) and in the quality record, so the run says it in
plain words; the losing meshes are deleted before the workspace goes home.

A mesh that is still broken after this is the engine's failing on the shape, and the fallback
ladder moves the run on to the next engine (pipeline/engine_fallback: a broken mesh moves on).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A valid mesh whose worst face is more than this many degrees out of square is near the edge:
#: the corpus' cfMesh meshes that came out broken on one run read 82-86 on the others, and the
#: delivered ones mostly sit under 65 (median 60 in the cfm-after set).
NEAR_EDGE_NON_ORTHO = 75.0
#: Only a candidate this many degrees better than the best so far displaces it (checkMesh's
#: figure moves by a few hundredths between two reads of the same mesh).
MIN_GAIN_DEG = 0.5
#: The biggest mesh a rescue is tried on: each candidate is a whole extra polyMesh on the run's
#: disk - in memory on Cloud Run (16-32 GiB jobs), about 1.2 GB raw for 6.7 M cells - beside the
#: one kept. The mitral ventricle base came out invalid at 5.2 M cells once 5 layers were asked.
RESCUE_MAX_CELLS = 6_000_000
#: ...and the biggest one re-meshed: a second cartesianMesh holds its own working set on top.
#: Between the two, only the optimiser is tried.
REMESH_MAX_CELLS = 4_000_000
#: A re-mesh is started only when the time left holds this many times the first run's mesher time.
REMESH_HEADROOM = 1.3
#: improveMeshQuality's passes: cfMesh's defaults are 50 loops of 50 iterations and 2 surface
#: iterations; more surface iterations let it move the wall points a fold sits on.
OPTIMISE_ARGS = "-nLoops 50 -nIterations 50 -nSurfaceIterations 5"
#: The boundary-layer optimiser's settings for the protective re-mesh (cfMesh's defaults:
#: nSmoothNormals 5, maxNumIterations 5, featureSizeFactor 0.3, relThicknessTol 0.1). cfMesh
#: refuses a featureSizeFactor or relThicknessTol outside (0, 1).
PROTECTIVE_LAYER_OPTIMISATION = {"nSmoothNormals": 10, "maxNumIterations": 10,
                                 "featureSizeFactor": 0.15, "relThicknessTol": 0.05}
#: The record beside the mesh.
RESCUE_FILE = "mesh_rescue.json"
#: The scratch directory candidates live in while the rescue runs; never shipped.
SCRATCH = "_rescue"

_POLYMESH = Path("constant") / "polyMesh"
#: The name the protective re-mesh is recorded and kept under.
REMESHED = "re-meshed protecting the layers"


def near_edge(q: dict | None) -> str:
    """Why this mesh is at its validity edge, in words ('' when it is not)."""
    q = q or {}
    fatal = list(q.get("fatal") or [])
    if fatal:
        return "checkMesh found " + ", ".join(str(f) for f in fatal)
    no = _num(q.get("max_non_ortho"))
    if no is not None and no > NEAR_EDGE_NON_ORTHO:
        return (f"its worst cell is {no:.1f} degrees out of square (over "
                f"{NEAR_EDGE_NON_ORTHO:.0f})")
    return ""


def rank(q: dict | None) -> tuple:
    """The order candidates are kept in, lower first: no fatal defect, then the lowest worst
    non-orthogonality, then the fewest highly skewed faces."""
    q = q or {}
    no = _num(q.get("max_non_ortho"))
    return (1 if q.get("fatal") else 0, 180.0 if no is None else no,
            int(_num(q.get("skew_faces")) or 0))


def measured(q: dict | None) -> bool:
    """Whether `q` is a COMPLETE checkMesh reading: a cell count and a worst angle. checkMesh
    that died early still comes back as a record with no fatal defect listed - and an absent
    defect is no proof the mesh has none."""
    q = q or {}
    cells, no = _num(q.get("cells")), _num(q.get("max_non_ortho"))
    return bool(cells and cells > 0 and no is not None)


def better(cand: dict | None, best: dict | None) -> bool:
    """Whether `cand` measures better than `best` by a margin worth a swap. A candidate without a
    complete reading never wins."""
    if not measured(cand):
        return False
    a, b = rank(cand), rank(best)
    if a[0] != b[0]:
        return a[0] < b[0]
    return a[1] < b[1] - MIN_GAIN_DEG


def protective_meshdict(text: str) -> str | None:
    """The meshDict with the boundary-layer optimiser set to protect the layers
    (PROTECTIVE_LAYER_OPTIMISATION) and layer untangling on; None when the dict has no
    boundaryLayers block (nothing to protect). Any optimisationParameters already there are
    replaced, everything else is kept byte for byte."""
    m = re.search(r"^boundaryLayers\s*\n\{", text, flags=re.M)
    if not m:
        return None
    start = m.end()
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    if depth:
        return None
    body = text[start:i - 1]
    body = re.sub(r"\n\s*optimisationParameters\s*\{[^{}]*\}", "", body)
    params = " ".join(f"{k} {v:g};" if isinstance(v, float) else f"{k} {v};"
                      for k, v in PROTECTIVE_LAYER_OPTIMISATION.items())
    extra = f"    optimisationParameters {{ {params} }}\n"
    if not re.search(r"^\s*untangleLayers\s", body, flags=re.M):
        extra += "    untangleLayers 1;\n"
    if not re.search(r"^\s*optimiseLayer\s", body, flags=re.M):
        extra += "    optimiseLayer 1;\n"
    body = body.rstrip() + "\n" + extra
    return text[:start] + body + text[i - 1:]


def rescue(workspace, q: dict, *, bashrc: str, deadline: float, mesher_seconds: float,
           check_seconds: float = 60.0,
           measure: Callable[..., dict] | None = None,
           runner: Callable[..., Any] | None = None, env: dict | None = None,
           is_2d: bool = False) -> dict:
    """The quality record of the mesh left in `workspace` after the rescue: `q` itself when the
    mesh is not near its edge (or there is no time or room to try), else the best measured
    candidate's record, with `rescue` saying what was tried and kept. `deadline` is the monotonic
    time no lever - nor the checkMesh that measures it - may run past; `mesher_seconds` and
    `check_seconds` are the first cartesianMesh's and the first checkMesh's wall times."""
    why = near_edge(q)
    if not why:
        return q
    ws = Path(workspace)
    record: dict = {"trigger": why, "before": _brief(q), "tried": [], "kept": "as built"}
    cells = _num(q.get("cells")) or 0
    if is_2d:
        return _done(ws, q, {**record, "skipped": "2D case (cartesian2DMesh) - not rescued"})
    if cells > RESCUE_MAX_CELLS:
        return _done(ws, q, {**record, "skipped": (
            f"{int(cells):,} cells is over the {RESCUE_MAX_CELLS:,} a rescue may copy")})
    if not (ws / _POLYMESH / "owner").exists():
        return q
    from meshpipeline.engines.cfmesh.foam_exec import _foam_env, check_mesh
    from meshpipeline.sandbox.safe_exec import run_guarded
    check: Callable[..., dict] = measure if measure is not None else check_mesh
    run: Callable[..., Any] = runner if runner is not None else run_guarded
    if env is None:
        env = _foam_env()

    meshdict = ws / "system" / "meshDict"
    try:
        asked_dict = meshdict.read_text() if meshdict.exists() else ""
    except OSError:
        return q                       # nothing is moved before the case can be read back
    kept_dict = asked_dict
    scratch = ws / SCRATCH
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    best_q, best_dir, best_name = q, scratch / "as_built", "as built"
    #: what one measurement of a candidate may take: a checkMesh like the first, with room
    check_s = 1.5 * max(float(check_seconds or 0.0), 10.0) + 15.0
    logger.warning("cfMesh mesh at its validity edge (%s) - trying the engine's own levers", why)

    def _left() -> float:
        """Seconds a lever may still run: to the deadline, less the check that must follow it."""
        return deadline - time.monotonic() - check_s

    def _measure(name: str) -> dict | None:
        (ws / "mesh_quality.json").unlink(missing_ok=True)     # never a cached figure
        if deadline - time.monotonic() < check_s:
            logger.warning("rescue: no time left to measure the %s mesh - not a candidate", name)
            return None
        try:
            return check(ws, bashrc=bashrc) or None
        except Exception:  # noqa: BLE001 - an unmeasured candidate is no candidate
            logger.warning("rescue: checkMesh of the %s mesh failed", name, exc_info=True)
            return None

    def _consider(name: str, cand_q: dict | None, step: dict) -> None:
        nonlocal best_q, best_dir, best_name, kept_dict
        step.update(_brief(cand_q) if cand_q else {"measured": False})
        record["tried"].append(step)
        if cand_q and better(cand_q, best_q):
            new_dir = scratch / name.replace(" ", "_")
            shutil.rmtree(new_dir, ignore_errors=True)
            (ws / _POLYMESH).rename(new_dir)
            shutil.rmtree(best_dir, ignore_errors=True)
            best_q, best_dir, best_name = cand_q, new_dir, name
            kept_dict = meshdict.read_text() if meshdict.exists() else kept_dict
        else:
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)

    def _optimise(src_dir: Path, name: str) -> None:
        budget = min(_left(), max(300.0, mesher_seconds))
        if budget < 60.0:
            record["tried"].append({"step": name, "skipped": "no time left in the run cap"})
            return
        shutil.copytree(src_dir, ws / _POLYMESH)
        t = time.monotonic()
        rc = _run(run, ws, bashrc, env, f"improveMeshQuality {OPTIMISE_ARGS}",
                  "improveMeshQuality.log", budget)
        step = {"step": name, "rc": rc, "seconds": round(time.monotonic() - t, 1)}
        if rc != 0 or not (ws / _POLYMESH / "owner").exists():
            step["error"] = _tail(ws / "improveMeshQuality.log")
            record["tried"].append(step)
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
            return
        _consider(name, _measure(name), step)

    moved = False
    try:
        # the mesh as built is the first candidate, kept aside while the others are made
        (ws / _POLYMESH).rename(best_dir)
        moved = True
        # 1. the engine's optimiser on the mesh as built
        _optimise(best_dir, "optimised")
        # 2. a protective re-mesh, when the best is still near the edge and the time holds a run
        protective = protective_meshdict(asked_dict) if asked_dict else None
        if near_edge(best_q) and protective is not None:
            if cells > REMESH_MAX_CELLS:
                record["tried"].append({"step": REMESHED, "skipped": (
                    f"{int(cells):,} cells is over the {REMESH_MAX_CELLS:,} a second run may "
                    "hold beside the first")})
            elif _left() < REMESH_HEADROOM * max(mesher_seconds, 30.0):
                record["tried"].append({"step": "re-meshed protecting the layers",
                                        "skipped": "no time left in the run cap for another run"})
            else:
                meshdict.write_text(protective)
                t = time.monotonic()
                rc = _run(run, ws, bashrc, env, "cartesianMesh", "cartesianMesh_rescue.log",
                          _left())
                step = {"step": "re-meshed protecting the layers", "rc": rc,
                        "seconds": round(time.monotonic() - t, 1)}
                if rc == 0 and (ws / _POLYMESH / "owner").exists():
                    remesh_q = _measure("re-meshed")
                    remesh_dir = scratch / "remeshed_raw"
                    if remesh_q and near_edge(remesh_q):
                        # keep the raw re-mesh aside, optimise it too, and let all three compete
                        (ws / _POLYMESH).rename(remesh_dir)
                        record["tried"].append({**step, **_brief(remesh_q)})
                        if better(remesh_q, best_q):
                            shutil.rmtree(best_dir, ignore_errors=True)
                            best_q, best_dir, best_name = remesh_q, remesh_dir, REMESHED
                            kept_dict = protective
                        _optimise(remesh_dir, "re-meshed protecting the layers, then optimised")
                        if best_dir != remesh_dir:
                            shutil.rmtree(remesh_dir, ignore_errors=True)
                    else:
                        _consider("re-meshed protecting the layers", remesh_q, step)
                else:
                    step["error"] = _tail(ws / "cartesianMesh_rescue.log")
                    record["tried"].append(step)
                    shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
    except Exception:  # noqa: BLE001 - a lever must never lose the mesh already built
        logger.warning("cfMesh rescue failed part-way - keeping the best mesh so far",
                       exc_info=True)
    finally:
        if moved:
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
            best_dir.rename(ws / _POLYMESH)
            if meshdict.exists() or kept_dict:
                # the meshDict beside the mesh is the one that built it
                meshdict.write_text(kept_dict)
        shutil.rmtree(scratch, ignore_errors=True)
    record["kept"] = best_name
    return _done(ws, best_q, record)


def _done(ws: Path, q: dict, record: dict) -> dict:
    record["after"] = _brief(q)
    record["note"] = note(record)
    try:
        (ws / RESCUE_FILE).write_text(json.dumps(record, default=str))
    except OSError:
        pass
    logger.warning("cfMesh rescue: %s", record["note"])
    return {**q, "rescue": record}


def note(record: dict) -> str:
    """The rescue in the engineer's words."""
    before, after = record.get("before") or {}, record.get("after") or {}
    head = f"The first cfMesh mesh was at its validity edge: {record.get('trigger')}."
    if record.get("skipped"):
        return f"{head} No second try: {record['skipped']}."
    tried = [s["step"] for s in record.get("tried") or [] if not s.get("skipped")]
    if record.get("kept") == "as built":
        return (f"{head} Tried {', then '.join(tried) or 'nothing (no time left)'}; none measured "
                "better, so the mesh as built is kept.")
    return (f"{head} Kept the {record.get('kept')} mesh: "
            f"{_words(before)} before, {_words(after)} after.")


def _words(b: dict) -> str:
    fatal = b.get("fatal") or []
    no = b.get("max_non_ortho")
    s = f"worst cell {no:.1f} degrees" if isinstance(no, (int, float)) else "no figure"
    return s + (f" and {', '.join(fatal)}" if fatal else ", no fatal defect")


def _brief(q: dict | None) -> dict:
    q = q or {}
    return {k: q.get(k) for k in ("cells", "max_non_ortho", "max_skewness", "skew_faces", "fatal")}


def _run(runner, ws: Path, bashrc: str, env: dict, cmd: str, log_name: str,
         timeout: float) -> int:
    try:
        with (ws / log_name).open("w") as fh:
            proc = runner(["bash", "-lc", f"source {bashrc} >/dev/null 2>&1 && {cmd}"],
                          cwd=str(ws), env=env, stdout=fh, stderr=subprocess.STDOUT,
                          timeout=max(1, int(timeout)))
        return int(proc.returncode)
    except subprocess.TimeoutExpired:
        return -1


def _tail(path: Path, n: int = 6) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["MIN_GAIN_DEG", "NEAR_EDGE_NON_ORTHO", "PROTECTIVE_LAYER_OPTIMISATION",
           "REMESH_MAX_CELLS", "RESCUE_FILE", "RESCUE_MAX_CELLS", "better", "near_edge", "note",
           "protective_meshdict", "rank", "rescue"]
