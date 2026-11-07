# Responsibility: Build an FEA mesh for one builder attempt: measure the staged solid, ask the AI once for its judgement calls (engines/fea_ai: checked, made exact by the rules), compute the plan, check its element count before meshing, run the engine adapter, and pull the lever ladder when a measured check fails.
# Boundaries: repeatable. It authors nothing a model could get wrong - the model only picks from declared choices, through the AI decision cache, and no answer means the rules' own plan - and it publishes nothing itself (what the user reads comes through finalize and the manifest).
# Collaborates with: agents/builder/invoke.py (calls drive through EngineSpec.build_driver_for), engines/fea_plan.py, engines/fea_size_field.py, engines/fea_ladder.py, engines/gmsh/fea_adapter.py (run in the mesh runner), contracts/decision_record.py (every decision cited), engines/preflight.py (the stop before meshing).
"""THE FEA DRIVER (FEA build step 4).

One attempt:

1. measure the staged solid (cad/fea_measure on geometry.step, the metre B-rep every gmsh run
   meshes), so Face_k in the plan is Face_k in the mesh and in the deck;
2. PLAN-fea-17: an assembly (several bodies) is staged as its parts, fused where they are bonded or
   side by side where they only touch, every face named Part_k_Face_j;
3. compute the plan (engines/fea_plan.py) and its element estimate;
4. PLAN-fea-8: over the detail level, stop BEFORE meshing, with the options (a pre-flight
   refusal: no mesher is started, no time is spent);
5. mesh it (the engine adapter, in the mesh runner), then read the measured checks;
6. a check failed: the next lever of the fixed ladder (engines/fea_ladder.py), at most
   MAX_PASSES passes in the one attempt;
7. LAD-fea-5: the best pass is delivered.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshpipeline.engines import bucket_files as BF
from meshpipeline.engines import fea_ai as FA
from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_part_sizes as PS
from meshpipeline.engines import fea_plan as P

if TYPE_CHECKING:
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

ENGINE = "gmsh"
#: Failure markers this driver ends an attempt with.
NO_SOURCE = "fea_source_missing"
BUILD_FAILED = "fea_build_failed"
#: The mesher ran past its guard clock: our infrastructure, not the part (no lever is pulled).
GUARD_TRIP = "fea_guard_trip"
#: The marker of a mesh built but too big for the worker to collect (kept in the exchange).
TOO_BIG = "fea_too_big_for_worker"
#: RUN-4: the marker, and the gate, of a pass OUR side never ran (its machine could not be started,
#: its result never came back): ours, never the part's - no lever is pulled.
NOT_RUN = "fea_not_run"
NOT_RUN_GATE = "fea_dispatch"
#: The gate a guard trip is reported under (ours, never the part's).
GUARD_GATE = "fea_guard"
#: MESH-2: the gate a mesh that failed AT a flagged CAD spot is reported under.
CAD_DEFECT_GATE = "fea_cad_defect"
#: The plan measures every wall of a part up to this many surface triangles (the geometry check
#: stops at cad/fea_measure.MAX_RAY_TRIANGLES for speed; the plan cannot hold an unmeasured wall).
MAX_RAY_TRIANGLES = 5_000_000
#: What the remote mesher receives besides the plan: the solid and the facts the deck reads.
PAYLOAD = (*P.STAGED_SOLIDS, "cad_face_names.json", P.PLAN_FILE, "gmsh_spec.json", "assembly.brep", "assembly.json")
#: Where each pass's deliverables are kept until the best one is chosen.
PASS_FOLDER = "_fea_pass_{n}"  # noqa: S105 - a folder name, not a secret
#: every file a pass writes that the delivery reads: the solver files ride with their mesh, so the
#: pass restored is restored whole (a later, worse pass never leaves its .bdf / .cdb behind)
DELIVERABLES = ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.msh", "quality.json", P.PLAN_FILE, "gmsh_spec.json")
#: The zones alone past this many elements are past the limit: the histogram stops counting.
STOP_ELEMENTS = 1.5 * P.ELEMENT_LIMIT


# #
# measure and plan (in the worker, pure but for the reading of the staged solid)
# #

def staged_solid(ws: Path) -> Path | None:
    """PLAN-fea-15: the staged solid the FEA path reads - gmsh's own B-rep file when the staging
    wrote one, else the STEP; None when neither is there."""
    return next((ws / n for n in P.STAGED_SOLIDS if (ws / n).exists()), None)


def _metre_box(step: Path) -> tuple[list[float], float]:
    """The staged solid's box in metres, as gmsh reads it (the reader every gmsh run uses)."""
    import gmsh

    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
        gmsh.model.add("fea_box")
        gmsh.model.occ.importShapes(str(step))
        gmsh.model.occ.synchronize()
        box = list(gmsh.model.getBoundingBox(-1, -1))
        gmsh.model.remove()
    finally:
        if mine:
            gmsh.finalize()
    diag = math.dist(box[:3], box[3:])
    return box, diag


#: MESH-8 FOR A SINGLE PART: the skins its staged solid was opened without, and each face's CAD origin (the
#: workspace file the mesher reads to name every face for the CAD face it came from)
SKINS_FILE = "skins.json"
#: the staged solid as the CAD drew it, kept beside the opened one
CAD_SOLID = "geometry_cad.brep"


def open_single_skins(ws: Path) -> dict | None:
    """MESH-8 FOR A SINGLE PART: a part whose staged solid has skins of no thickness (cad/zero_skins - a pocket
    or hole cut exactly to a wall, its floor left lying on that wall) is staged again with each skin left out and
    its outline opened through the face it lay on, as an assembly's parts are (cad/fea_assembly.stage). Writes the
    opened solid as geometry.brep (the drawn one kept as CAD_SOLID) and SKINS_FILE: {"origin": the CAD face of
    each face of the opened solid, "faces": the CAD's face count, "skins": [{"part", "faces", "on", "pairs"}]}.
    None when there is nothing to open (no skin, several bodies, a file not read); {"note"} when the opened copy was
    not sound - the part is then meshed as drawn, said. Never raises."""
    import shutil

    from OCP.BRepTools import BRepTools

    from meshpipeline.cad import fea_assembly as A
    from meshpipeline.cad import zero_skins as Z
    from meshpipeline.cad.scout import read_cad

    try:
        if (ws / SKINS_FILE).exists() and (ws / CAD_SOLID).exists():
            # ALREADY OPENED (this workspace driven again): the staged solid is the opened one; its record stands
            return json.loads((ws / SKINS_FILE).read_text())
        staged = staged_solid(ws)
        if staged is None:
            return None
        parts = A.solids(read_cad(staged))
        if len(parts) != 1:
            return None
        opened = Z.open_skins(parts[0])
        if opened.note:
            return {"note": opened.note}
        if not opened.skins:
            return None
        n = len(Z._faces(parts[0]))
        if staged.suffix == ".brep":
            shutil.copyfile(staged, ws / CAD_SOLID)
        else:
            BRepTools.Write_s(parts[0], str(ws / CAD_SOLID))
        BRepTools.Write_s(opened.solid, str(ws / "geometry.brep"))
        pairs = [A.skin_pair(f"Face_{k.skin}", f"Face_{k.on}") for k in opened.skins]
        info = {"origin": [int(o) for o in opened.origin], "faces": n,
                "skins": [{"part": "The part", "faces": [p["face"] for p in pairs],
                           "on": sorted({p["on"] for p in pairs}, key=lambda f: int(f.rsplit("_", 1)[-1])),
                           "pairs": pairs}]}
        (ws / SKINS_FILE).write_text(json.dumps(info, indent=1))
        return info
    except Exception as exc:  # noqa: BLE001 - meshed as drawn, as before MESH-8 reached single parts
        logger.warning("fea driver: a single part's skins were not opened (%s: %s)", type(exc).__name__, exc)
        return {"note": f"its skins could not be opened ({type(exc).__name__})"}


def named_as_cad(record: dict, surface, info: dict) -> dict:
    """The measurement of an opened single part (open_single_skins) said in the CAD's own face names: each face
    Face_k of the opened solid renamed to the CAD face it came from, every skin face kept as a face of the part
    (kind "skin", the face it lies on) so the CAD's face count stands, and the skins recorded. Returns the record."""
    from meshpipeline.cad import fea_assembly as A

    origin = [int(o) for o in info.get("origin") or []]
    names = [f"Face_{o}" for o in origin]
    out = A.rename_record(record, names)
    out["face_names"] = record.get("face_names") or "Face_k"
    if len(surface.names) == len(names):
        surface.names = list(names)
    faces = dict(out.get("faces") or {})
    for sk in info.get("skins") or []:
        for pr in sk.get("pairs") or []:
            faces[str(pr["face"])] = {"kind": "skin", "skin_on": pr.get("on"), "words": pr.get("words")}
    out["faces"] = faces
    out["skins"] = list(info.get("skins") or [])
    out.setdefault("notes", [])
    out["notes"] = [*out["notes"], *A.skin_notes(out["skins"])]
    return out


def solids_from_shells(ws: Path) -> int:
    """MESH-1: A FILE OF FACES THAT CLOSE is read as the solid it bounds: a STEP or IGES written as closed
    shells with no solid entity (aircraft and keel surfaces, 24 Oct gate) - the shells are made solids,
    sewn first only within the clean check's own tight tolerance (cad/validity_brep), nothing else
    touched. Writes geometry.brep and returns how many solids it made (0: nothing to do, or no shell
    closes - the part is then meshed, or refused, as it stands)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    from meshpipeline.cad.scout import read_cad
    from meshpipeline.cad.validity_brep import SEW_TOLERANCE, _bbox, _sew

    step = staged_solid(ws)
    if step is None:
        return 0
    shape = read_cad(step)
    if TopExp_Explorer(shape, TopAbs_SOLID).More():
        return 0
    lo, hi = _bbox(shape)
    if lo is None:
        return 0
    diag = float(math.dist(tuple(lo), tuple(hi)))
    sewn = _sew(shape, SEW_TOLERANCE * diag) or shape
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    made = 0
    ex = TopExp_Explorer(sewn, TopAbs_SHELL)
    while ex.More():
        shell = TopoDS.Shell_s(ex.Current())
        ex.Next()
        if not BRep_Tool.IsClosed_s(shell):
            continue
        mk = BRepBuilderAPI_MakeSolid(shell)
        if mk.IsDone():
            b.Add(comp, mk.Solid())
            made += 1
    if made:
        BRepTools.Write_s(comp, str(ws / "geometry.brep"))
    return made


def measure_staged(ws: Path) -> tuple[dict, Any, float]:
    """(the measurement record in mm, its labelled surface in mm, the part's diagonal in m)."""
    from meshpipeline.cad.fea_measure import measure_cad_surface
    from meshpipeline.cad.scout import read_cad

    step = staged_solid(ws) or ws / "geometry.step"
    _box, diag_m = _metre_box(step)
    shape = read_cad(step)
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    # THE TIGHT BOX: OpenCASCADE's plain box is padded by the shapes' tolerances, about 6% of an 8 mm
    # nut - every length of a small part's record (walls, radii, the cusps' places) came out short
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    diag_read = math.dist((x0, y0, z0), (x1, y1, z1))
    if diag_read <= 0 or diag_m <= 0:
        raise ValueError("the staged solid has no size")
    # the staged STEP is in metres whatever unit its header names: the scale is read off the two
    # readers' boxes, so the record is millimetres either way - and a unit is a power of ten, so the
    # ratio is snapped to one (the two boxes never agree to the last digit)
    ratio = 1000.0 * diag_m / diag_read
    mm_per_unit = 10.0 ** round(math.log10(ratio)) if ratio > 0 else ratio
    # THE SAME SOLID, MEASURED BEFORE (cad/fea_measure_cache, keyed on the staged file's digest): its record is
    # read back and only the surface the plan sizes on is tessellated again - no walls ray-traced, no Boolean
    # checks run a second time (a landing gear's plan spent 1054 s of its 1090 s measurement on those)
    from meshpipeline.cad import fea_measure_cache as MC

    cache_key = MC.key_for(step, mm_per_unit=mm_per_unit, max_ray_triangles=MAX_RAY_TRIANGLES)
    kept = MC.load(cache_key)
    if kept is not None:
        surface = _surface_again(shape, mm_per_unit, healed=bool(kept.get("healed")))
        if surface is not None:
            logger.info("fea driver: the staged solid was measured before - its record reused")
            return kept["record"], surface, diag_m
    healed = False
    try:
        record, surface = measure_cad_surface(shape, mm_per_unit=mm_per_unit, max_ray_triangles=MAX_RAY_TRIANGLES)
    except Exception as exc:  # noqa: BLE001 - healed and measured again; a second failure is the part's
        # PLAN-fea-15: A SHAPE THE MEASURE CANNOT READ is healed (OpenCASCADE's ShapeFix: wires,
        # edges, faces, shells) and measured again, so the part still gets a plan
        from OCP.ShapeFix import ShapeFix_Shape

        logger.warning("fea driver: the staged solid could not be measured as read (%s) - healing it", exc)
        fix = ShapeFix_Shape(shape)
        fix.Perform()
        record, surface = measure_cad_surface(fix.Shape(), mm_per_unit=mm_per_unit,
                                              max_ray_triangles=MAX_RAY_TRIANGLES)
        record.setdefault("notes", []).append(f"the CAD was healed before it was measured ({exc})")
        healed = True
    # MESH-1: WHERE THE CAD MAY BE BROKEN, measured on this very solid (detect only, never a stop): the
    # mesh is tried; a valid one is delivered with a note, and only a failure AT that spot is the CAD's
    try:
        from meshpipeline.cad import suspect_defects as SD
        record["suspects"] = SD.find(shape, record)
    except Exception as exc:  # noqa: BLE001 - no suspects measured is no suspects
        logger.warning("fea driver: suspected defects not measured (%s)", type(exc).__name__)
    MC.save(cache_key, {"record": record, "healed": healed})
    return record, surface, diag_m


def staged_mm_per_unit(step: Path) -> float:
    """The staged solid's millimetres per unit as OpenCASCADE reads it - measure_staged's own reading (the two
    readers' boxes, the ratio snapped to a power of ten) - for the shell run (SHSTAGE-6)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    from meshpipeline.cad.scout import read_cad

    _box, diag_m = _metre_box(step)
    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(read_cad(step), b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    diag_read = math.dist((x0, y0, z0), (x1, y1, z1))
    if diag_read <= 0 or diag_m <= 0:
        raise ValueError("the staged solid has no size")
    ratio = 1000.0 * diag_m / diag_read
    return 10.0 ** round(math.log10(ratio))


#: SHSTAGE-6: THE SHELL RUN's time limit, seconds (the extraction takes up to about ten minutes on the biggest
#: corpus parts, the shell mesh up to about nine) - and never more than half of what the attempt has left, so
#: the solid mesh beside it keeps its time.
SHELL_RUN_LIMIT_S = 2400
SHELL_GATE = "fea_shell"


MIXED_GATE = "fea_mixed"


def mesh_as_of(state) -> str:
    """SHSTAGE-5: "shell" when the user's words asked for a shell (settled at Proceed), "mixed" from a confirmation
    stored before shell + solid left the stage (MIXED-4), else "solid" (state fea_mesh_as)."""
    got = str((state or {}).get("fea_mesh_as") or "").strip().lower()
    return got if got in ("shell", "mixed") else "solid"


async def shell_pass(ws: Path, state, *, job_id: str, run=None, target: dict | None = None) -> dict | None:
    """SHSTAGE-6: when the stage settled Mesh as: Shell, the staged solid's shell mesh and its solver files
    (engines/shell_run, in a process of its own), beside the solid deck. The solid path is unchanged: a shell
    that cannot be made is said, and the solid mesh is still delivered. None when Solid was settled.
    MIXED-5: when Auto settled Mixed, the same in engines/mixed_run (thin walls shell, thick regions solid, joined).
    `target`: the element count the user asked for (element_target) - the shell's sizes follow it as the solid's do
    (SHMESH-23)."""
    kind = mesh_as_of(state)
    if kind == "solid":
        return None
    import subprocess
    import sys

    from meshpipeline.engines import mixed_run, shell_run

    mod = mixed_run if kind == "mixed" else shell_run

    step = staged_solid(ws)
    limit = float(SHELL_RUN_LIMIT_S)
    left = attempt_left_s(run)
    if left is not None:
        limit = max(120.0, min(limit, 0.5 * left))
    try:
        argv = [sys.executable, "-m", mod.__name__, str(ws), str(step), "staged"]
        if kind == "shell" and target and int(target.get("elements") or 0) > 0:
            argv.append(json.dumps({k: target[k] for k in ("elements", "mode", "low", "high") if k in target}))
        proc = await asyncio.to_thread(
            subprocess.run, argv,
            capture_output=True, text=True, timeout=limit, check=False)
        rec = mod.read(ws)
        if not rec:
            rec = {"made": False, "why": f"the {kind} run stopped (exit {proc.returncode})"}
    except subprocess.TimeoutExpired:
        rec = {"made": False, "why": f"it took longer than the {int(limit) // 60} minutes allowed"}
    except Exception as exc:  # noqa: BLE001 - the solid mesh is still delivered
        rec = {"made": False, "why": f"it could not be started ({type(exc).__name__})"}
    if not rec.get("words"):
        rec["words"] = mod.words(rec)
        (ws / mod.RECORD).write_text(json.dumps(rec, indent=1, sort_keys=True, default=float))
    if kind == "mixed":
        c = rec.get("counts") or {}
        _cite(job_id, [{"stage": MIXED_GATE, "decided": "mixed mesh" if rec.get("made") else "no mixed mesh",
                        "value": (int(c.get("shell_elements") or 0) + int(c.get("solid_elements") or 0)) or None,
                        "rule": "MIXED-5",
                        "inputs": {k: rec.get(k) for k in ("thin_share", "too_thick", "regions", "seconds", "why")},
                        "words": rec["words"]}])
        return rec
    _cite(job_id, [{"stage": SHELL_GATE, "decided": "shell mesh" if rec.get("made") else "no shell mesh",
                    "value": rec.get("elements"), "rule": "SHSTAGE-6",
                    "inputs": {k: rec.get(k) for k in ("bodies", "open_edges", "solid_bodies", "walls_mm", "seconds",
                                                         "why", "target")},
                    "words": rec["words"]}])
    return rec


def _surface_again(shape, mm_per_unit: float, *, healed: bool):
    """The labelled surface measure_cad_surface measured `shape` on (healed first when it was), tessellated
    again; None when it cannot be (the solid is then measured as before)."""
    from meshpipeline.cad.fea_measure import _brep_surface

    try:
        if healed:
            from OCP.ShapeFix import ShapeFix_Shape

            fix = ShapeFix_Shape(shape)
            fix.Perform()
            shape = fix.Shape()
        return _brep_surface(shape, float(mm_per_unit))[0]
    except Exception as exc:  # noqa: BLE001 - measured as before
        logger.info("fea driver: the kept measurement's surface could not be made again (%s)", type(exc).__name__)
        return None


def _suspect_faces(record: dict) -> set[str]:
    return {str(f) for s in record.get("suspects") or [] for f in s.get("faces") or []}


def defect_at_failure(record: dict, passes: list[dict]) -> dict | None:
    """MESH-2: the failure is the CAD's when the faces the mesher failed on are the faces a check
    flagged (gmsh's own error names them, or the invalid elements lie on them); None otherwise."""
    flagged = _suspect_faces(record)
    if not flagged:
        return None
    import re

    hit = sorted({str(f) for p in passes for f in p.get("bad_faces") or []} & flagged,
                 key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    if not hit:
        return None
    sus = [s for s in record.get("suspects") or [] if set(s.get("faces") or []) & set(hit)]
    if record.get("assembly_plan"):
        # AN ASSEMBLY: parts passing into each other are what the tie is for (PLAN-fea-17), never a CAD
        # repair. Only ONE PART that is itself broken - two measures agree, on that one solid - is the
        # CAD's (fc_glass_frames, 25 Oct: one pair of frame members passing into each other ended as a
        # stop after 4 fused passes and 170 min)
        sus = [s for s in sus if s.get("confirmed") and int(s.get("body") or 0) > 0]
        if not sus:
            return None
        hit = [f for f in hit if any(f in (s.get("faces") or []) for s in sus)]
    return {"faces": hit, "suspects": sus, "confirmed": any(s.get("confirmed") for s in sus)}


def crossing_located(record: dict, state: dict | None) -> dict | None:
    """LAD-fea-16: faces of the CAD already located as crossing each other, before meshing - the geometry
    check's self-overlap confirmed by its two independent measures (cad/suspect_defects), or the repair
    inspection's self-intersection (pipeline/repair_inspect, cad/repair). None when neither found one."""
    faces = sorted({str(f) for s in record.get("suspects") or []
                    if s.get("kind") == "self_overlap" and s.get("confirmed") for f in s.get("faces") or []})
    report = (((state or {}).get("repair_report") or {}).get("report") or {})
    crossing = sum(int(d.get("count") or 1) for d in report.get("defects") or []
                   if str(d.get("code")) == "self_intersection")
    if not faces and not crossing:
        return None
    return {"faces": faces, "repair_self_intersections": crossing,
            "by": " and ".join(w for w, on in (("the geometry check", bool(faces)),
                                              ("the repair inspection", bool(crossing))) if on)}


def crossing_words(found: dict, n: int) -> str:
    where = f" ({', '.join(found['faces'][:6])}{', ...' if len(found['faces']) > 6 else ''})" if found["faces"] else ""
    return (f"Pass {n}'s fill failed, and {found['by']} had already found faces of the CAD crossing each other{where}. "
            "Another gmsh algorithm cannot mend a crossing in the CAD, so the part goes straight to fTetWild, which "
            "meshes through it (LAD-fea-16, LAD-fea-9).")


def suspect_note(record: dict) -> str:
    """The note a delivered mesh carries when a check flagged a spot (MESH-1)."""
    from meshpipeline.cad import suspect_defects as SD
    sus = record.get("suspects") or []
    if not sus:
        return ""
    seen, said = set(), []
    for s in sus:
        key = (s.get("kind"), tuple(sorted(s.get("faces") or [])))
        if key not in seen:
            seen.add(key)
            said.append(SD.words(s))
    return (" ".join(said[:3]) + " The mesh was built and is valid; if your solver misbehaves there, check that "
            "spot in your CAD.")


def connection_of(state) -> str:
    """How an assembly's parts are joined, as the user answered on the stage (state fea_connection):
    bonded unless they said contact."""
    from meshpipeline.cad.fea_assembly import BONDED, CONTACT

    said = str((state or {}).get("fea_connection") or "").strip().lower()
    return CONTACT if said == CONTACT else BONDED


def gap_joins_of(state) -> bool:
    """PLAN-fea-18: whether parts across a small gap are joined (state fea_gap_joins), on unless the
    user switched it off on the stage."""
    said = (state or {}).get("fea_gap_joins")
    return True if said is None else bool(said)


def pairs_of(state) -> dict:
    """PLAN-fea-19/20: the user's choices for single pairs (state fea_pairs, by pair key), checked;
    empty when the stage set none - every pair then follows the rule."""
    from meshpipeline.cad import fea_pairs as FP

    return FP.clean_overrides((state or {}).get("fea_pairs") or {})


def prepare_assembly(ws: Path, record: dict, surface, *, connection: str, source_path: str = "",
                     gap_joins: bool = True, pairs: dict | None = None) -> dict:
    """PLAN-fea-17: an assembly's record for the plan, and its staged file for the mesher. Every
    face is renamed Part_k_Face_j (on the record and on the surface the size field reads), each part
    gets its own base size, and the parts are staged as the connection asks (cad/fea_assembly.stage).
    Returns the record (the same dict, renamed in place)."""
    import numpy as np

    from meshpipeline.cad import fea_assembly as A
    from meshpipeline.cad.scout import read_cad

    staged = staged_solid(ws) or ws / "geometry.step"
    shape = read_cad(staged)
    parts = A.solids(shape)
    names: list[str] = [""] * len(parts)
    notes: list[str] = []
    if source_path and Path(source_path).exists():
        try:
            names = A.match_body_names(A.read_body_names(source_path), parts)
        except Exception as exc:  # noqa: BLE001 - a courtesy: every part is still Part_k
            logger.info("fea driver: the CAD's part names were not read (%s)", exc)
    global_names = A.global_face_names(shape)
    renamed = A.rename_record(record, global_names)
    record.clear()
    record.update(renamed)
    if len(surface.names) == len(global_names):
        surface.names = list(global_names)
    sidecar = A.stage(shape, connection, ws, names=names, gap_joins=gap_joins, pairs=pairs)
    # each part's own size, read off the measured surface (millimetres): its faces' triangles
    bases = []
    for p in range(1, len(parts) + 1):
        faces = [n for n in record.get("faces") or {} if A.part_of_face(n) == p]
        labels = [k for k, n in enumerate(surface.names) if A.part_of_face(n) == p]
        tri = surface.tris[np.isin(surface.label, labels)] if labels else np.zeros((0, 3), dtype=np.int64)
        if not len(tri) or not faces:
            continue
        pts = surface.xyz[np.unique(tri)]
        diag = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
        bases.append({"n": p, "id": A.part_id(p), "name": names[p - 1] if p - 1 < len(names) else "",
                      "diag_mm": round(diag, 6), "faces": sorted(faces, key=lambda f: int(f.rsplit("_", 1)[-1]))})
    record["part_bases"] = bases
    # MESH-8: the zero-thickness skins each part is meshed without (absent when there is none)
    skins = [sk for sk in sidecar.get("skins") or [] if sk.get("faces")]
    record["assembly_plan"] = {**({"skins": skins} if skins else {}), "staged": A.STAGED, "sidecar": A.SIDECAR, "connection": connection,
                               "mode": sidecar["mode"], "parts": sidecar["parts"], "pairs": len(sidecar["pairs"]),
                               "interfaces": len(sidecar["interfaces"]), "notes": notes + list(sidecar["notes"]),
                               # TIED FROM THE START: too many parts pass into each other to fuse (TIE_FIRST_PIECES)
                               "tie_first": sidecar.get("tie_first"),
                               # WHERE THE PARTS MEET, for the AI's joint suggestion (engines/fea_ai AI-fea-7)
                               "pair_list": [{"pair": i.get("id"), "kind": i.get("kind"), "parts": i.get("parts"),
                                              "area_mm2": i.get("area_mm2"), "at_mm": i.get("at_mm")}
                                             for i in sidecar["interfaces"]],
                               # PLAN-fea-19/20: every pair's type and every overlap's owner, with the reason
                               "joints": list(sidecar.get("joints") or []),
                               "joint_counts": dict(sidecar.get("joint_counts") or {})}
    if sidecar.get("joins"):
        # PLAN-fea-18: how the joints were found, the small-gap joints, and the faces no gap sizing
        # should read (the gap a joint joins, a skin of no thickness)
        record["assembly_plan"]["joins"] = sidecar["joins"]
    return record


def skin_words(skins: list[dict]) -> list[str]:
    from meshpipeline.cad.fea_assembly import skin_notes
    return skin_notes(skins)


def open_pairs_decision(ws: Path) -> list[dict]:
    """PLAN-fea-21's why-record entry for the delivered mesh: how many bonded pairs meshed apart were
    checked and which are OPEN (no mesh node within the reach on a face) - [] for a single part or a
    mesh with no tied pair."""
    from meshpipeline.cad import fea_pairs as FP

    try:
        q = json.loads((ws / "quality.json").read_text())
    except (OSError, ValueError):
        return []
    asm = ((q.get("fea") or {}).get("assembly") or {}) if isinstance(q, dict) else {}
    checked = asm.get("bonded_check") or {}
    if not checked.get("checked") and not asm.get("open_bonded"):
        return []
    opened = [o for o in asm.get("open_bonded") or [] if isinstance(o, dict)]
    return [{"stage": "fea_check", "decided": "open bonded pairs", "value": len(opened), "rule": FP.OPEN_RULE,
             "inputs": {"checked": checked.get("checked"), "closed": checked.get("closed"),
                        "open": [o.get("pair") or o.get("name") for o in opened]},
             "words": ("; ".join(FP.open_words(o) for o in opened[:6]) if opened
                       else f"Every one of the {checked.get('checked')} tied bonded pair(s) has mesh nodes within its "
                            "reach on every face.")}]


def restage_tied(ws: Path, record: dict) -> dict:
    """PLAN-fea-17's way on for a bonded assembly no pass could mesh as one: the same parts staged
    apart and tied where they touch (cad/fea_assembly.stage, tie); the record's assembly says so."""
    from meshpipeline.cad import fea_assembly as A
    from meshpipeline.cad.scout import read_cad

    shape = read_cad(staged_solid(ws) or ws / "geometry.step")
    try:
        names = [str(p.get("name") or "") for p in json.loads((ws / A.SIDECAR).read_text()).get("parts") or []]
    except (OSError, ValueError):
        names = []
    try:
        before = json.loads((ws / A.SIDECAR).read_text())
    except (OSError, ValueError):
        before = {}
    sidecar = A.stage(shape, A.BONDED, ws, names=names, tie=True, gap_joins=before.get("gap_joins", True) is not False,
                      pairs=before.get("pair_choices") or None)
    plan = dict(record.get("assembly_plan") or {})
    plan.update(mode=sidecar["mode"], pairs=len(sidecar["pairs"]), notes=list(sidecar["notes"]),
                joints=list(sidecar.get("joints") or []), joint_counts=dict(sidecar.get("joint_counts") or {}))
    record["assembly_plan"] = plan
    return record


#: PLAN-fea-8: A COUNT THAT RAN OUT OF CUBES while still under the limit is counted again with four times the
#: cubes, up to this many; still short there, its floor under the limit is taken as fitting (engines/fea_plan.
#: finish: a plan short of its machine steps up when it runs, RUN-3). A plan that fits is never coarsened for a
#: count that merely stopped short (the corpus sweep of 27 Oct: 18 plans coarsened so; job
#: bf863215's 6.19M-element plan cut to 3.83M with its bolt fillets at 3 elements per quarter-turn, not 6).
MAX_CUBES_CEILING = 12_000_000


def walls_of(record: dict) -> dict[str, float]:
    """PLAN-fea-31: every face's measured wall in metres (the wall most of it agrees on, else its median)."""
    out: dict[str, float] = {}
    for name, row in (record.get("faces") or {}).items():
        row = row or {}
        t = row.get("thickness_wall_mm") or row.get("thickness_median_mm") or row.get("thickness_min_mm")
        if t is None:
            continue
        try:
            v = float(t)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            out[str(name)] = v * 1e-3
    return out


def _histogram(surface, zone_list: list[dict], levers: list[dict], diag_m: float, *, limit: float | None = None,
               recount: bool = True, scale: float = 1.0, walls: dict[str, float] | None = None,
               base_m: float | None = None):
    """The zones' volume histogram (engines/fea_size_field). With `limit`, it is counted to 1.5 times it
    (never less than STOP_ELEMENTS), and a count that ran out of cubes under the limit is counted again
    with more (MAX_CUBES_CEILING). `scale`: PLAN-fea-27's factor on the base size."""
    from meshpipeline.engines import fea_size_field as SF

    field = SF.SizeField(surface, zone_list, growth=L.growth(levers), scale_to_m=1e-3, walls=walls)
    coarsest = float(base_m) if base_m else P.base_size(diag_m, scale)
    stop = STOP_ELEMENTS if limit is None else max(STOP_ELEMENTS, 1.5 * float(limit))
    cubes = SF.MAX_CUBES
    while True:
        hist = field.histogram(coarsest=coarsest, per_h3=P.PER_H3, stop_elements=stop, max_cubes=cubes)
        if hist.complete or hist.stopped != "cubes" or limit is None or not recount or cubes >= MAX_CUBES_CEILING:
            return hist
        if P.elements(hist, coarsest, P.PER_H3) > P.LIMIT_AIM * float(limit):
            return hist                     # past the limit already: counting further changes nothing
        cubes = min(MAX_CUBES_CEILING, 4 * cubes)


def make_plan(record: dict, surface, diag_m: float, *, levers: list[dict] | None = None,
              limit: int = P.ELEMENT_LIMIT, zone_floor_m: float | None = None, hist=None,
              through: float | None = None, ai: dict | None = None,
              zone_floors: dict[str, float] | None = None, recount: bool = True,
              scale: float | None = None, beyond: float | None = None, sizes: dict | None = None) -> dict:
    """The plan for this part, with the ladder's levers applied: the zones the rules ask for, the
    coarsest base the part allows, the estimate against `limit`. `zone_floor_m`: PLAN-fea-8's
    coarsening (the finest zones floored, never past their validity minimums); `hist`: the zones'
    histogram when the caller already has it; `ai`: the AI's checked decision (engines/fea_ai), made
    exact by the rules before any coarsening. `zone_floors`: PLAN-fea-8's coarsening kind by kind (zone
    kind -> floor), the low-stress zones first (COARSEN_ORDER). `scale`: PLAN-fea-27's one factor on
    every size (the base and every zone, the minimums re-applied) for the user's element count; beyond:
    PLAN-fea-30's factor past those minimums, as far as valid elements allow. sizes: PLAN-fea-29's sizes the
    user asked for, part by part (engines/fea_part_sizes.per_part), held before any count moves the rest."""
    levers = list(levers or [])
    zone_list, applied = FA.apply_zones(P.zones(record, tighten=L.tightened(levers)), _used(ai), record,
                                        diag_m=diag_m)
    s, u = float(scale or 1.0), float(beyond or 1.0)
    if sizes:
        zone_list = P.user_sized(zone_list, sizes)
    if s != 1.0 or u != 1.0:
        zone_list = P.scaled(zone_list, s, u)
    base_m = P.base_size(diag_m, s, u)
    ub = P.user_base(sizes or {})
    if ub:
        base_m = ub if not record.get("part_bases") else max(base_m, ub)
    for kind, f in sorted((zone_floors or {}).items()):
        zone_list = P.floored(zone_list, float(f), through or P.KEEP_THROUGH, kinds=(kind,))
    if zone_floor_m:
        zone_list = P.floored(zone_list, float(zone_floor_m), through or P.KEEP_THROUGH)
    if hist is None:
        hist = _histogram(surface, zone_list, levers, diag_m, limit=limit, recount=recount, scale=s,
                          walls=walls_of(record), base_m=base_m)
    plan = P.finish(record, zone_list, hist, diag_m=diag_m, limit=limit, levers=levers, growth=L.growth(levers),
                    scale=s, beyond=u, base_m=base_m)
    if sizes:
        plan["user_sizes"] = {k: dict(v) for k, v in sorted(sizes.items())}
    if zone_floors:
        plan["zone_floors_m"] = {k: float(v) for k, v in sorted(zone_floors.items())}
    if zone_floor_m:
        plan["zone_floor_m"] = float(zone_floor_m)
    if zone_floor_m or zone_floors:
        plan["resolution_short"] = P.shortfalls(zone_list)
    plan["mesh"] = L.mesh_settings(levers)
    if record.get("skins"):
        plan["skins"] = list(record["skins"])           # MESH-8, a single part's (an assembly's ride in plan["assembly"])
    if ai:
        plan["ai"] = {**ai, "applied": applied}
        _with_ai(plan)
    # WHAT THE RESULT SCREEN SHOWS (engines/fea_results.py): every face, and every hot spot where it is
    plan["faces"] = [{"name": n, "kind": (r or {}).get("kind"), "area_mm2": (r or {}).get("area_mm2"),
                       "thickness_min_mm": (r or {}).get("thickness_min_mm")}
                      for n, r in sorted((record.get("faces") or {}).items(), key=lambda kv: P._face_order(kv[0]))]
    plan["hot_spots"] = [{k: h.get(k) for k in ("id", "type", "shape", "faces", "radius_mm", "angle_deg",
                                                "length_mm", "depth_mm", "local_thickness_mm", "capped")
                           if h.get(k) is not None}
                          | {"at_m": [round(float(c) * 1e-3, 9) for c in (h.get("at_mm") or [])]}
                          for h in record.get("hot_spots") or []]
    plan["measure"] = {"faces": len(record.get("faces") or {}), "counts": record.get("counts") or {},
                       "thinnest_wall_mm": (record.get("thickness") or {}).get("min_mm"),
                       "bodies": record.get("bodies"), "notes": list(record.get("notes") or [])}
    return plan


#: PLAN-fea-8's coarsening search: the zone floor doubles from the finest zone this many times at
#: most, then one step back where that still fits.
FLOOR_STEPS = 14
#: PLAN-fea-8: THE ORDER A PLAN OVER ITS LIMIT IS COARSENED IN, kind by kind - the low-stress bulk first,
#: the stress raisers last (27 Oct): rounds (sized for their shape only), capped sharp corners (their
#: stress never settles), walls and the parts' own sizes, then holes, fillets and notches.
COARSEN_ORDER: tuple[tuple[str, ...], ...] = (("round",), ("corner",), ("wall", "wall_spot", "part"),
                                              ("hole", "fillet", "notch"))


def _coarsen_group(record: dict, surface, diag_m: float, *, levers: list[dict], limit: int, ai, group,
                   zone_list: list[dict], floors_done: dict[str, float], sizes: dict | None = None):
    """One step of PLAN-fea-8's coarsening: the first floor for `group`'s zones (the others as `floors_done`
    already floors them) under which the plan fits, found by halving the list of floors - each twice the
    last, from the group's finest zone, while it still changes a zone's size - then one step back where
    that still fits. Returns (the plan, the floors now in force, whether it fits), or None when no floor
    changes the group."""
    def floors_with(f: float) -> dict[str, float]:
        return {**floors_done, **dict.fromkeys(group, f)}

    base = [dict(z) for z in zone_list]
    for kind, f in sorted(floors_done.items()):
        base = P.floored(base, f, kinds=(kind,))
    floor = min(float(z["size_m"]) for z in base if z["kind"] in group)
    floors: list[float] = []
    last: list[float] | None = None
    for _ in range(FLOOR_STEPS):
        floor *= 2.0
        got = [float(z["size_m"]) for z in P.floored(base, floor, kinds=group)]
        if got == last:
            break
        last = got
        floors.append(floor)
    if not floors:
        return None
    tried: dict[int, dict] = {}

    def at(k: int) -> dict:
        if k not in tried:
            tried[k] = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes, recount=False,
                                 zone_floors=floors_with(floors[k]))
        return tried[k]

    if not at(len(floors) - 1)["fits"]:
        # this kind at its validity minimums is not enough: kept there, and the next kind is coarsened
        return at(len(floors) - 1), floors_with(floors[-1]), False
    lo, hi = 0, len(floors) - 1                       # floors[hi] fits; find the first that does
    while lo < hi:
        mid = (lo + hi) // 2
        if at(mid)["fits"]:
            hi = mid
        else:
            lo = mid + 1
    best, f = at(hi), floors[hi]
    finer = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes, zone_floors=floors_with(0.75 * f),
                      recount=False)
    if finer["fits"]:
        best, f = finer, 0.75 * f
    return best, floors_with(f), True


def complexity(record: dict) -> str:
    """The part's complexity class, for its fill rate (PLAN-fea-13): measured, never guessed - the
    CAD faces, the hot spots and the micro details. Complex geometry builds slower per element than
    simple geometry of the same element count (Areen, 21 Oct)."""
    faces = len(record.get("faces") or {})
    spots = len(record.get("hot_spots") or [])
    micro = len(record.get("micro") or [])
    if faces > COMPLEX_FACES or spots > COMPLEX_SPOTS or micro:
        return "complex"
    if faces <= SIMPLE_FACES and spots <= SIMPLE_SPOTS:
        return "simple"
    return "moderate"


def fill_rate(record: dict) -> tuple[float, str]:
    """(elements per second of one native pass at the slow end of the part's class, the class)."""
    cls = complexity(record)
    return FILL_RATES[cls], cls


def memory_elements(memory_gib: float | None = None) -> int:
    """PLAN-fea-16: the most elements the mesher's peak memory, with MEMORY_MARGIN to spare, fits in
    `memory_gib` - by the measured model peak = MEMORY_BASE_MB + BYTES_PER_ELEMENT x elements."""
    gib = MESH_JOB_MEMORY_GIB if memory_gib is None else float(memory_gib)
    room_mb = gib * 1024.0 / MEMORY_MARGIN - MEMORY_BASE_MB
    return max(0, int(room_mb * 1024.0 * 1024.0 / BYTES_PER_ELEMENT))


def _ceiling_gib() -> float:
    from meshpipeline.contracts import mesh_machine as MM
    return MM.ceiling_gib()


def element_limit(record: dict, ceiling_s: float | None = None) -> tuple[int, str]:
    """PLAN-fea-8's limit for this part, and why: what the LARGEST machine allowed holds (RUN-1, Areen
    24 Oct: a bigger machine replaces shrinking a mesh to fit - the plan is sized by the accuracy rules,
    and only a plan past the largest machine is coarsened, the last resort). NEVER LESS FOR TIME
    (PLAN-fea-13): `ceiling_s` is kept for the callers' signature and decides nothing."""
    from meshpipeline.contracts import mesh_machine as MM
    limit = memory_elements(MM.ceiling_gib())
    return limit, (f"what the largest machine allowed holds ({MM.CEILING.name}, {MM.CEILING.memory_gib:g} GiB; "
                   f"{BYTES_PER_ELEMENT:g} bytes an element, with {MEMORY_MARGIN:g} times to spare)")


#: WHAT THE MESH JOB WRITES BESIDE THE MESHER, per element: the solver files (about 1,600 bytes) and
#: their archive (about 700), measured on the lab VMs (adapters/mesh_execution/cloud_run_client's FEA
#: result sizes). Cloud Run keeps them in memory; a VM keeps them on disk.
WORKSPACE_BYTES_PER_ELEMENT = 2300.0


def memory_need(elements: float) -> tuple[float, float]:
    """(the mesher's peak in GiB, with MEMORY_MARGIN; the workspace's files in GiB) for a plan of
    `elements` (PLAN-fea-16's measured model)."""
    n = max(0.0, float(elements))
    process = MEMORY_MARGIN * (MEMORY_BASE_MB * 1024.0 * 1024.0 + BYTES_PER_ELEMENT * n) / 1024.0 ** 3
    return process, WORKSPACE_BYTES_PER_ELEMENT * n / 1024.0 ** 3


def limit_facts() -> dict:
    """PLAN-fea-16's numbers, for the why record."""
    return {"memory_gib": MESH_JOB_MEMORY_GIB, "bytes_per_element": BYTES_PER_ELEMENT,
            "base_mb": MEMORY_BASE_MB, "margin": MEMORY_MARGIN, "memory_elements": memory_elements(),
            "ceiling_elements": memory_elements(_ceiling_gib())}


def _used(ai: dict | None) -> dict | None:
    """The AI's decision when it is in use (engines/fea_ai: status "used"), else None."""
    return (ai or {}).get("decision") if (ai or {}).get("status") == "used" else None


def _with_ai(plan: dict) -> dict:
    """The AI's words and why-record entries after the rules' own (AI-fea-n)."""
    ai = plan.get("ai")
    if ai:
        plan["words"] = [*(plan.get("words") or []), *FA.words(ai, plan)]
        plan["decisions"] = [*(plan.get("decisions") or []), *FA.decisions(ai)]
    return plan


def element_target(state: dict | None, ai: dict | None) -> dict | None:
    """AI-fea-4 / PLAN-fea-27: the element count the user named, as the AI read it from their words and the
    rules checked it (engines/fea_ai._target): {"elements", "mode", "low"/"high" for a range, "source": "ai"};
    None when it named none. The AI alone reads the notes (a saved decision answers for it when it is down,
    AI-fea-6). When the AI gave no answer and the user left notes, {"unread": True}: the plan is the rules'
    own and the words say a count in the notes was not applied this run."""
    d = _used(ai)
    if d is None:
        return {"unread": True} if (state or {}).get("user_notes") else None
    n = int(d.get("target_elements") or d.get("budget_elements") or 0)
    if not n:
        return None
    mode = str(d.get("target_mode") or "about")
    out: dict = {"elements": n, "mode": mode, "quote": "", "source": "ai"}
    if mode == "range":
        out.update(low=int(d["target_min"]), high=int(d["target_max"]))
    return out


#: PLAN-fea-27: a count that falls slower than s^-FLAT_SLOPE as the factor grows is held by the minimums
FLAT_SLOPE = 0.3


def _gap(n: float, t: float) -> float:
    """How far an estimate is from the count, as a log ratio (0 = on it)."""
    return abs(math.log(max(float(n), 1.0) / max(float(t), 1.0)))


def _solve(at, lo: float, hi: float, n_lo: float, n_hi: float, aim: float, hit, steps: int) -> list:
    """PLAN-fea-27/30's search on ONE factor x whose count falls as x grows, with f(lo) = n_lo over the aim and
    f(hi) = n_hi under it: the line through the bracket's ends (log count against log x), kept inside the bracket
    (its geometric middle when the line leaves it or is flat), until an estimate is a hit or the steps run out.
    Returns [(x, count, plan)] tried."""
    tried: list = []
    side = [0, 0]                   # how many steps in a row moved the low end, the high end
    for _ in range(steps):
        if hi / lo < 1.0 + 1e-3:
            break
        ln_lo, ln_hi = math.log(max(n_lo, 1.0)), math.log(max(n_hi, 1.0))
        slope = (ln_hi - ln_lo) / (math.log(hi) - math.log(lo))
        x = math.exp(math.log(lo) + (math.log(aim) - ln_lo) / slope) if slope < -1e-6 else math.sqrt(lo * hi)
        if not (lo * (1 + 1e-6) < x < hi * (1 - 1e-6)) or max(side) >= 2:
            # the line keeps landing on one side (a count far from straight in log-log): halve the bracket
            x = math.sqrt(lo * hi)
            side = [0, 0]
        plan = at(x)
        n = float(plan["estimate"]["elements"])
        tried.append((x, n, plan))
        if hit(n):
            break
        if n > aim:
            lo, n_lo = x, n
            side = [side[0] + 1, 0]
        else:
            hi, n_hi = x, n
            side = [0, side[1] + 1]
    return tried


def _full_scale(zone_list: list[dict]) -> float:
    """PLAN-fea-27: the factor past which no zone moves (each at its accuracy minimum) - the base reaches its
    fifth of the part at 2."""
    out = TARGET_FULL
    for z in zone_list:
        cap = P.scale_cap(z)
        if cap is not None and not z.get("user") and float(z["size_m"]) > 0:
            out = max(out, cap[0] / float(z["size_m"]))
    return min(out, 1.0 / P.SCALE_MIN) * (1.0 + 1e-6)


def beyond_full(zone_list: list[dict], base_m: float, diag_m: float) -> float:
    """PLAN-fea-30: the second factor past which nothing moves - every zone at its validity limit (a curve at 3 per
    quarter-turn, a part's own base at a third of the part) or, with no such limit (a wall), at a third of the whole
    part, and the base at a third of the part. The search runs all the way there: only validity stops a count."""
    top = float(diag_m) / P.VALID_DIVISIONS
    out = top / float(base_m) if base_m else 1.0
    for z in zone_list:
        if z.get("user") or float(z.get("size_m") or 0.0) <= 0.0:
            continue
        vc = P.valid_cap(z)
        out = max(out, min(vc[0] if vc else top, top) / float(z["size_m"]))
    return max(1.0, out) * (1.0 + 1e-6)


#: PLAN-fea-27: the base reaches PLAN-fea-28's fifth at twice its tenth
TARGET_FULL = P.MIN_DIVISIONS / P.TARGET_MIN_DIVISIONS


def target_plan(record: dict, surface, diag_m: float, *, levers: list[dict], ai: dict | None, target: dict,
                limit: int, rules: dict, sizes: dict | None = None, correction: float | None = None) -> dict:
    """PLAN-fea-27/30: the plan sized to the user's element count. `rules` is the rules' own plan (its estimate
    N0). Over the count, in this order, each solved on the REAL estimate (the size field's histogram):
      1. ONE factor s on every size - the base (to a fifth of the part) and every zone - inside the accuracy
         minimums (2 through every wall, 4 per quarter-turn of every curve, the corner cap);
      2. still over: a second factor past those minimums, as far as valid elements allow (PLAN-fea-30: 3 per
         quarter-turn of every curve, 3 elements across every part) - the minimums crossed are named;
      3. still over at the validity limits: that plan, the lowest this part can go, and said so.
    Under the count, s < 1: finer, never past the largest machine (the count is aimed at LIMIT_AIM of `limit` at
    most). The count only ever moves the sizes the user did not set (PLAN-fea-29: their sizes per part come
    first). The factors are monotone in the count, so a smaller count never plans more. `correction`: built over
    planned on a pass already built of this count (PLAN-fea-31's re-aim) - the estimates are read times it. Returns
    the plan with its `target` record."""
    asked = int(target["elements"])
    mode = str(target.get("mode") or "about")
    # the most the largest machine holds, with the solve's own tolerance to spare (an estimate near it still fits)
    cap = int(P.LIMIT_AIM * float(limit) / (1.0 + P.TARGET_TOLERANCE))
    aim: float = min(asked, cap)
    if mode == "range":
        # A RANGE: aimed at its middle, and any estimate inside it is a hit (the solve stops there)
        low, high = int(target["low"]), int(target["high"])
        ok_lo, ok_hi = float(min(low, cap)), float(min(high, int(P.LIMIT_AIM * float(limit))))
        if ok_lo >= ok_hi:
            ok_lo, ok_hi = aim / (1.0 + P.TARGET_TOLERANCE), aim * (1.0 + P.TARGET_TOLERANCE)
    else:
        ok_lo, ok_hi = aim / (1.0 + P.TARGET_TOLERANCE), aim * (1.0 + P.TARGET_TOLERANCE)

    k = float(correction or 1.0)

    def hit(n: float) -> bool:
        return ok_lo <= n * k <= ok_hi

    def miss(n: float) -> float:
        """How far outside what counts as a hit (0 inside)."""
        return 0.0 if hit(n) else min(_gap(n * k, ok_lo), _gap(n * k, ok_hi))

    n0 = float(rules["estimate"]["elements"])
    rec = {"asked": asked, "mode": mode, "source": target.get("source"), "quote": target.get("quote") or "",
           "aim": aim, "machine": aim < asked or (mode == "range" and int(target["low"]) > cap),
           "rules_elements": int(n0), "rules_base_m": rules["base_size_m"],
           **({"sizes_first": True} if sizes else {}), **({"correction": round(k, 4)} if k != 1.0 else {}),
           **{k: target[k] for k in ("low", "high") if target.get(k)}}
    if ((mode == "at_most" and n0 * k <= asked) or (mode == "at_least" and n0 * k >= asked)
            or (mode == "range" and int(target["low"]) <= n0 * k <= int(target["high"]) and rules["fits"])):
        rules["target"] = {**rec, "scale": 1.0, "kept_rules": True, "steps": 0}
        return rules

    def at(s: float, u: float = 1.0) -> dict:
        return make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, scale=s, beyond=u, sizes=sizes)

    tried: list[tuple[float, float, float, dict]] = [(1.0, 1.0, n0, rules)]       # (s, u, count, plan)
    lowest = False
    aim = aim / k                                   # the estimate that builds the count (PLAN-fea-31's re-aim)
    if not hit(n0) and n0 < aim:
        # FINER: s under 1, down to SCALE_MIN
        p_min = at(P.SCALE_MIN)
        n_min = float(p_min["estimate"]["elements"])
        tried.append((P.SCALE_MIN, 1.0, n_min, p_min))
        if n_min > aim and not hit(n_min):
            tried += [(x, 1.0, n, p) for x, n, p in _solve(at, P.SCALE_MIN, 1.0, n_min, n0, aim, hit, P.TARGET_STEPS)]
    elif not hit(n0):
        # 1. COARSER INSIDE THE ACCURACY MINIMUMS
        zs = FA.apply_zones(P.zones(record, tighten=L.tightened(levers)), _used(ai), record, diag_m=diag_m)[0]
        if sizes:
            zs = P.user_sized(zs, sizes)
        s_full = _full_scale(zs)
        p_a = at(s_full)
        n_a = float(p_a["estimate"]["elements"])
        tried.append((s_full, 1.0, n_a, p_a))
        if not hit(n_a) and n_a < aim:
            tried += [(x, 1.0, n, p) for x, n, p in _solve(at, 1.0, s_full, n0, n_a, aim, hit, P.TARGET_STEPS)]
        elif not hit(n_a):
            # 2. PAST THE ACCURACY MINIMUMS, to the validity limits
            base_a = P.base_size(diag_m, s_full)
            u_full = beyond_full(p_a.get("zones") or [], base_a, diag_m)
            if u_full > 1.0 + 1e-3:
                p_b = at(s_full, u_full)
                n_b = float(p_b["estimate"]["elements"])
                tried.append((s_full, u_full, n_b, p_b))
                if not hit(n_b) and n_b < aim:
                    tried += [(s_full, x, n, p) for x, n, p in _solve(lambda u: at(s_full, u), 1.0, u_full, n_a, n_b,
                                                                      aim, hit, P.TARGET_STEPS)]
                elif not hit(n_b):
                    lowest = True              # 3. even at the validity limits: the lowest this part can go
            else:
                lowest = True
    fitting = [x for x in tried if x[3]["fits"]] or tried
    if lowest:
        s, u, n, best = max(fitting, key=lambda x: (x[1], x[0]))
    else:
        s, u, n, best = min(fitting, key=lambda x: (miss(x[2]), _gap(x[2], aim), x[0], x[1]))
    rec["aim"] = int(round(aim * k))
    zs = best.get("zones") or []
    best["target"] = {**rec, "scale": round(float(s), 4), "beyond": round(float(u), 4), "steps": len(tried) - 1,
                      "hit": hit(n), "lowest": bool(lowest and not hit(n)),
                      "held_by_minimums": False,
                      "crossed": sorted({str(z["crossed"]) for z in zs if z.get("crossed") and not z.get("user")}),
                      "base_capped": bool(s > 1.0 and u <= 1.0 and P.base_size(diag_m, s) < P.base_size(diag_m) * s * (1 - 1e-9)),
                      "tried": [[round(x[0], 4), round(x[1], 4), int(x[2])] for x in tried]}
    return best


def choose_plan(record: dict, surface, diag_m: float, *, levers: list[dict] | None = None,
                ceiling_s: float | None = None, ai: dict | None = None, target: dict | None = None,
                sizes: dict | None = None, correction: float | None = None) -> dict:
    """PLAN-fea-8, NEVER A STOP BEFORE MESHING (Areen, 21 Oct): the rules' own plan when it fits under
    the limit; else the finest zones coarsened until it fits, never past what keeps elements valid,
    what that leaves short named (resolution_short); else - every zone at its validity minimum and
    still over - meshed so anyway. The returned plan always meshes.
    `target`: the element count the user named (element_target): PLAN-fea-27 scales the whole plan to it,
    both ways, inside the minimums and the largest machine; only a scaled plan that still does not fit the
    machine goes on to PLAN-fea-8's coarsening, as any plan over its limit does."""
    levers = list(levers or [])
    limit, why = element_limit(record, ceiling_s)
    plan = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes)
    missed: dict | None = None
    # AI-fea-4: the AI gave no answer and the user left notes - a count in them is not applied, and said
    unread = bool(target and target.get("unread"))
    if unread:
        target = None
    if target:
        scaled_plan = target_plan(record, surface, diag_m, levers=levers, ai=ai, target=target, limit=limit,
                                  rules=plan, sizes=sizes, correction=correction)
        if scaled_plan["fits"]:
            scaled_plan["limit"].update(why=why, memory=limit_facts())
            return _restate(record, scaled_plan)
        missed = {k: v for k, v in scaled_plan["target"].items() if k != "tried"}
        plan = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes)
    if not plan["fits"] and FA.refines(_used(ai)):
        # AI-fea-5: THE AI'S REFINEMENTS NEVER COST THE RULES' OWN ZONES. Over the limit, they are
        # left out first (its other choices stand) - the limit's coarsening is only ever for what
        # the rules themselves need (lab, 24 Oct: three parts the rules meshed whole came back with
        # walls short of 2 elements because a refinement had pushed the plan over the limit)
        ai = FA.without_refines(ai or {})
        plan = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes)
    zone_list, _ = FA.apply_zones(P.zones(record, tighten=L.tightened(levers)), _used(ai), record, diag_m=diag_m)
    if sizes:
        zone_list = P.user_sized(zone_list, sizes)
    plan["limit"].update(why=why, memory=limit_facts())
    if missed:
        plan["target_missed"] = missed
    if unread:
        plan["target_unread"] = True
    if plan["fits"]:
        return _restate(record, plan)
    needed = int(plan["estimate"]["elements"])
    said = f"about {needed:,} elements" if plan.get("histogram_complete", True) else f"more than {needed:,} elements"
    # PLAN-fea-8, THE LOW-STRESS BULK FIRST (27 Oct): the zones are coarsened kind by kind in COARSEN_ORDER -
    # rounds kept only for their shape, then the capped corners (whose stress never settles), then the walls
    # and the parts' own sizes - and each kind only as far as it must; the stress raisers (fillets, holes,
    # notches) only when everything before them is at its validity minimum and the plan is still over
    floors_done: dict[str, float] = {}
    best = plan
    fitted = False
    kept: list[str] = []
    for n_group, group in enumerate(COARSEN_ORDER):
        sel = [z for z in zone_list if z["kind"] in group]
        if not sel:
            continue
        found = _coarsen_group(record, surface, diag_m, levers=levers, limit=limit, ai=ai, group=group,
                               zone_list=zone_list, floors_done=floors_done, sizes=sizes)
        if found is None:
            continue
        best, floors_done, fitted = found
        if fitted:
            kept = sorted({k for g in COARSEN_ORDER[n_group + 1:] for k in g
                           if any(z["kind"] == k for z in zone_list)})
            # GIVE BACK: the kinds coarsened to their minimums before this one fitted are refined again as
            # far as the plan still fits, the latest first (the stress raisers' floor, once found, stands)
            for back in reversed(COARSEN_ORDER[:n_group]):
                if not any(k in floors_done for k in back):
                    continue
                others = {k: v for k, v in floors_done.items() if k not in back}
                whole = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes, zone_floors=others,
                                  recount=False)
                if whole["fits"]:
                    best, floors_done = whole, others          # this kind needs no coarsening at all
                    continue
                again = _coarsen_group(record, surface, diag_m, levers=levers, limit=limit, ai=ai, group=back,
                                       zone_list=zone_list, floors_done=others, sizes=sizes)
                if again is not None and again[2]:
                    best, floors_done = again[0], again[1]
            break
    at_minimums = not fitted and not best["fits"]
    if at_minimums:
        # EVEN AT THEIR VALIDITY MINIMUMS the zones need more than the limit: meshed so anyway - the
        # least resolution that keeps every element valid - and said
        best["fits"] = True
    floor = max(floors_done.values()) if floors_done else 0.0
    if floors_done and not best.get("histogram_complete", True):
        # THE CHOSEN PLAN, COUNTED IN FULL ONCE (the search compared the candidates on the plain count)
        fits = best["fits"]
        best = make_plan(record, surface, diag_m, levers=levers, limit=limit, ai=ai, sizes=sizes, zone_floors=floors_done)
        best["fits"] = best["fits"] or fits or at_minimums
    best["limit"].update(why=why, memory=limit_facts())
    best["coarsened"] = {"needed": needed, "needed_words": said, "zone_floor_m": floor, "at_minimums": at_minimums,
                         # what was coarsened, kind by kind, and what kept the sizes its rules ask for
                         "floors_m": {k: float(v) for k, v in sorted(floors_done.items())}, "kept": kept,
                         # PLAN-fea-16: even the least valid mesh may not fit in the job's memory - meshed anyway
                         # past even the largest machine allowed: meshed anyway, said (RUN-2)
                         "over_memory": bool(at_minimums and int(best["estimate"]["elements"])
                                             > memory_elements(_ceiling_gib()))}
    if missed:
        best["target_missed"] = missed
    if unread:
        best["target_unread"] = True
    return _restate(record, best)


#: PLAN-fea-31: a built count further than this share from the count asked is re-aimed once
REAIM_SHARE = 0.10
#: PLAN-fea-31: the share of the measured built-over-planned (in log) the rebuild corrects by
REAIM_DAMP = 0.6


def closer_of_reaimed(passes: list[dict], asked) -> None:
    """PLAN-fea-31: after a re-aim, of the valid passes the one whose built count is closer to what was asked is
    delivered (the other is marked `off_count`, which LAD-fea-5 ranks after) - a re-aim never delivers a worse miss."""
    if not asked or not any(f.get("off_count") for f in passes):
        return
    valid = [f for f in passes if f.get("valid") and f.get("cells")]
    if len(valid) < 2:
        return
    best = min(valid, key=lambda f: _gap(float(f["cells"]), float(asked)))
    for f in valid:
        f["off_count"] = f is not best


def reaim_factor(plan: dict, facts: dict) -> float | None:
    """PLAN-fea-31: built over planned when a VALID pass of a plan sized to the user's count built a count off what
    they asked by more than REAIM_SHARE and another plan can move it (not the lowest valid plan, not the most the
    machine holds, not the rules' own plan kept); else None."""
    t = plan.get("target") or {}
    cells, est = int(facts.get("cells") or 0), int((plan.get("estimate") or {}).get("elements") or 0)
    if not t or not facts.get("valid") or not cells or not est or t.get("kept_rules") or t.get("lowest"):
        return None
    asked = float(t["asked"])
    mode = t.get("mode")
    if mode == "range":
        off = not (float(t["low"]) <= cells <= float(t["high"]))
    elif mode == "at_most":
        off = cells > asked * (1 + REAIM_SHARE)
    elif mode == "at_least":
        off = cells < asked / (1 + REAIM_SHARE)
    else:
        off = abs(cells / asked - 1.0) > REAIM_SHARE
    if not off or (t.get("machine") and cells < asked):
        return None
    # 0.6 OF THE MEASURED RATIO (in log): built over planned moves with the plan itself (a coarser plan reads a little
    # differently), and the full ratio overshot as often as it landed (corpus sweep, 6 Nov: -16% then +17%)
    return (cells / est) ** REAIM_DAMP


def _restate(record: dict, plan: dict) -> dict:
    """The plan's words and decisions again, now that it says what was coarsened."""
    plan["words"] = P.words(record, plan)
    note = suspect_note(record)
    if note:
        # MESH-1: the spot a check flagged, said beside the mesh (meshed anyway)
        plan["words"] = [*plan["words"], note]
    plan["decisions"] = P.decisions(record, plan)
    plan = _with_ai(plan)
    # AI-fea-9: how the sizes in the notes were tied to the parts, and what was not applied
    plan["words"] = [*plan["words"], *PS.words(plan.get("part_sizes"))]
    if plan.get("by_choice_failed"):
        plan["words"] = [*plan["words"], BY_CHOICE_WORDS[str(plan["by_choice_failed"])]]
    return plan


#: PLAN-fea-29/30: what is said when the plan the user's choices made could not be built valid
BY_CHOICE_WORDS = {
    "sizes": ("The sizes you asked for could not be built as a valid mesh on any pass, so this mesh is the rules' "
              "own plan instead; your sizes were not applied."),
    "count": ("The element count you asked for could not be built as a valid mesh on any pass, so this mesh is the "
              "rules' own plan instead; your count was not applied."),
}


def built_remark(ws: Path, record: dict | None = None) -> None:
    """PLAN-fea-27: the result's "From your notes" line says what was asked, planned AND built (the delivered
    pass's count), and whether the count was reached; the sizes per part too (PLAN-fea-29). The delivered plan's
    words say the built count as well (a plan at the lowest valid sizes that built under the count is not said to
    have missed it)."""
    from meshpipeline.contracts.user_notes import record_remark
    try:
        plan = json.loads((ws / P.PLAN_FILE).read_text())
        cells = int(json.loads((ws / "quality.json").read_text()).get("cells") or 0)
    except (OSError, ValueError, TypeError):
        return
    if plan.get("target") and cells and record is not None:
        plan["target"]["built"] = cells
        try:
            write_plan(ws, _restate(record, plan))
        except Exception:  # noqa: BLE001 - the words are a courtesy; the delivered mesh stands
            logger.warning("fea driver: the plan's words were not restated with the built count", exc_info=True)
    line = P.target_remark(plan, built=cells or None)
    if plan.get("by_choice_failed") == "count":
        line = BY_CHOICE_WORDS["count"]
    if line:
        record_remark(ws, line, by="fea_target")
    if plan.get("user_sizes") or (plan.get("part_sizes") or {}).get("status") not in (None, "none"):
        said = P.user_words(plan)[:1] or PS.words(plan.get("part_sizes"))[:1]
        if plan.get("by_choice_failed") == "sizes":
            said = [BY_CHOICE_WORDS["sizes"]]
        if said:
            record_remark(ws, said[0], by="fea_part_sizes")


#: PLAN-fea-13: THE FILL RATE BY COMPLEXITY - elements per second of one native gmsh pass, at the
#: slow end of each class, measured on the FEA lab VMs (8 vCPU, the mesh job's 16 GiB, gmsh on one
#: thread). MEASURED 22 Oct on 12 lab parts of 68k-1.3M elements, after the deck writers matched
#: every CAD face in one pass (they had sorted the whole mesh once per face: a 446-face sprocket
#: built at 165 a second, now 1,539): the slowest of each class was simple 1,779, moderate 1,243,
#: complex 1,539 elements a second. Each rate sits under its class's slowest with margin.
FILL_RATES: dict[str, float] = {"simple": 1500.0, "moderate": 1100.0, "complex": 1000.0}
#: the class boundaries (CAD faces, hot spots); any micro detail makes a part complex
SIMPLE_FACES, SIMPLE_SPOTS = 40, 20
COMPLEX_FACES, COMPLEX_SPOTS = 200, 100
#: PLAN-fea-16: THE MEMORY LIMIT. The mesh job's memory (deploy/gcp MESH_MEMORY: 32 GiB on dev and
#: prod, the Cloud Run most at 8 vCPU) and the mesher's peak, MEASURED on the lab VMs (22 Oct, the
#: gmsh driver's own VmHWM): 450 MB with nothing meshed, and 4.65 KB an element at 2.87M elements
#: (13.3 GB) and 5.04 KB at 2.71M (13.5 GB) - the tetrahedra, their curved midside nodes, the quality
#: arrays and the three solver files at once. BYTES_PER_ELEMENT is the worst of those, rounded up;
#: MEMORY_MARGIN covers the parts the lab did not see. With 32 GiB that is about 5.3M elements.
MESH_JOB_MEMORY_GIB = 32.0
MEMORY_BASE_MB = 450.0
BYTES_PER_ELEMENT = 5100.0
MEMORY_MARGIN = 1.25
#: PLAN-fea-13: NO GUESSED TIME LIMIT (Areen, 24 Oct). The mesher runs until it is done, and is
#: stopped only when it has shown NO SIGN OF LIFE for the stall window (engines/gmsh/gmsh_runner
#: FEA_STALL_WINDOW_S: no new output, no new step, no CPU burned - sandbox/liveness), or at the hard
#: ceiling of the machine it runs on.
#: THE CLOUD RUN JOB'S WALL: Cloud Run stops every run of the mesh job at its task timeout (deploy/gcp
#: MESH_TIMEOUT_SECONDS), a platform limit no setting here removes; less JOB_MARGIN_S for the hand-back.
#: A plan planned to pass it runs on a VM from the start, and a pass that meets it still working runs
#: again on a VM (RUN-5, contracts/mesh_machine.for_time / off_the_wall).
MESH_JOB_LIMIT_S = 14_400
JOB_MARGIN_S = 600
#: A VM HAS NO WALL. Its ceiling is only the backstop for a mesher that keeps printing but never
#: finishes; the stall watch is what stops a stuck one. Under the worker task's own 24-hour limit
#: (the run_simulation task's soft_time_limit, 86,400 s), with room to collect the result.
VM_CEILING_S = 20 * 3600
#: what an attempt keeps back after the last pass (the deck's writers, the hand-back)
ATTEMPT_RESERVE_S = 300


def ceiling_for(run=None, machine: dict | None = None) -> float:
    """PLAN-fea-13's hard ceiling for one pass: the wall of the machine it runs on - the Cloud Run
    job's limit less its margin, or VM_CEILING_S on a VM (RUN-5). NEVER what the attempt or the
    pipeline has left: a pass still making progress is not stopped by a clock (dev-areen 25 Oct: a
    pass cut at "the hard limit of 110 minutes", its retry given 1 minute). The pipeline's deadline
    moves on while the mesher makes progress (application/pipeline_budget.progress_extension), and
    the stall watch stops a stuck one. `run` is kept for the callers' signature and decides nothing."""
    if (machine or {}).get("where") == "gce":
        return float(VM_CEILING_S)
    return float(MESH_JOB_LIMIT_S - JOB_MARGIN_S)


def attempt_left_s(run) -> float | None:
    """What the attempt's own budget has left (its deadline, less ATTEMPT_RESERVE_S); None without one."""
    deadline = getattr(run, "deadline_s", None)
    started = getattr(run, "_started", None)
    if deadline and started is not None:
        return float(deadline) - (time.monotonic() - float(started)) - ATTEMPT_RESERVE_S
    return None


#: what an AI question keeps back of the attempt's time for the meshing still to come
AI_RESERVE_S = 900.0


def ai_wait_s(run) -> float:
    """The most an AI question may wait (engines/fea_ai): what the attempt has left after the
    meshing it still has to do - never past the attempt or the Cloud Run wall, whatever the model
    does. (A model answer is waited for; a mesher making progress is not cut - see ceiling_for.)"""
    left = attempt_left_s(run)
    base = ceiling_for(run) if left is None else max(60.0, min(ceiling_for(run), left))
    return max(0.0, base - AI_RESERVE_S)


def planned_seconds(plan: dict, rate: float | None = None) -> float | None:
    """The pass's planned time: its elements at the part's slow fill rate (PLAN-fea-13)."""
    amount = float((plan.get("estimate") or {}).get("elements") or 0)
    rate = rate if rate is not None else min(FILL_RATES.values())
    return amount / rate if rate and amount > 0 else None


def planned_work(plan: dict, *, rate: float | None = None, cls: str = "", ceiling_s: float) -> dict:
    """The pass's planned work, and how it is watched (PLAN-fea-13): the planned time at the part's
    slow fill rate is FOR THE RECORD ONLY - the mesher is stopped only after the stall window with no
    progress, or at `ceiling_s` (the wall of the machine it runs on: ceiling_for)."""
    from meshpipeline.engines.gmsh.gmsh_runner import stall_window

    amount = float((plan.get("estimate") or {}).get("elements") or 0)
    rate = rate if rate is not None else min(FILL_RATES.values())
    planned_s = amount / rate if rate and amount > 0 else None
    return {"where": "fea.gmsh", "unit": "elements", "amount": amount or None, "rate": rate, "class": cls,
            "planned_s": planned_s, "ceiling_s": int(ceiling_s), "stall_window_s": int(stall_window())}


#: LAD-fea-15: A PASS'S BUDGET IS SIZED FROM ITS ESTIMATE - this many times its planned time at the part's
#: slow fill rate (PLAN-fea-13), never under BUDGET_FLOOR_S, never over the machine's wall. Calibrated on the
#: corpus (docs/RULES.md LAD-fea-15): on this code, the slowest pass that built a mesh took under 3 times its
#: planned time at the fastest rate (1,500 a second), so no such pass is cut by it. A pass past it is the part's
#: slowness, not ours: the ladder goes on (a 20-hour wall let one surface grind for hours, 1 Nov).
BUDGET_FACTOR = 5.0
BUDGET_FLOOR_S = 3600.0


def pass_work(plan: dict, *, rate: float, cls: str, machine: dict | None) -> dict:
    """planned_work with LAD-fea-15's budget as its ceiling: `budget` is True when the budget, not the
    machine's wall, is what stops the pass."""
    wall = ceiling_for(None, machine)
    planned = planned_seconds(plan, rate)
    budget = min(wall, max(BUDGET_FLOOR_S, BUDGET_FACTOR * planned)) if planned else wall
    work = planned_work(plan, rate=rate, cls=cls, ceiling_s=budget)
    work["budget"] = bool(budget < wall)
    return work


def budget_words(work: dict, n: int, surface: int | None) -> str:
    where = f", meshing surface {surface}" if surface is not None else ""
    return (f"Pass {n} was still working{where} when the {int(work['ceiling_s']) // 60} minutes its size allows ran "
            "out, so it was stopped and the next way to mesh it is tried (LAD-fea-15).")


def watchdog_words(trip: dict) -> str:
    """What the watchdog saw, in plain words, for the why record and the refusal."""
    seen = trip.get("watchdog") or {}
    ran = int(round(float(seen.get("seconds") or 0) / 60.0))
    if trip.get("stalled"):
        quiet = int(round(float(seen.get("silence_at_stop_s") or trip.get("stall_window_s") or 0) / 60.0))
        return (f"the mesher made no progress for {quiet} minutes (no new output, no new step, no CPU "
                f"in use) after "
                f"{ran} minutes of work on pass {trip.get('pass')}, so it was stopped as stuck")
    return (f"the mesher was still working when it reached the hard limit of "
            f"{int(round(float(trip.get('ceiling_s') or 0) / 60.0))} minutes on pass {trip.get('pass')}")


def write_plan(ws: Path, plan: dict) -> None:
    (ws / P.PLAN_FILE).write_text(json.dumps(plan, indent=1, sort_keys=True))
    # the re-runnable record the deliverable has always carried, in the AI spec's own words
    (ws / "gmsh_spec.json").write_text(json.dumps({
        "element_order": int(plan["element"]["order"]),
        "size": {"mode": "absolute", "value": plan["base_size_m"]},
        "optimize": True}, indent=1))


# #
# the attempt
# #

def _cite(job_id: str, entries: list[dict]) -> None:
    from meshpipeline.contracts import decision_record as dr

    for e in entries:
        dr.cite(job_id, stage=str(e.get("stage")), decided=str(e.get("decided")), value=e.get("value"),
                rule=str(e.get("rule")), inputs=e.get("inputs") or {}, alternatives=e.get("alternatives") or (),
                source=str(e.get("source") or dr.SOURCE_RULE), words=str(e.get("words") or ""))


async def _narrate_pass(publish, *, n: int, first: bool, plan: dict, levers: list[dict], machine: dict,
                        budget_s: int) -> None:
    """Say a pass out loud before it meshes: on the first pass the plan's own decisions (their
    words, through the leak guard), on a later one why it runs again (the lever's words); then the
    machine it runs on, and the mesh start the run line counts from."""
    from meshpipeline.contracts import narration as N

    if first:
        for i, line in enumerate(N.plan_lines(plan.get("decisions"))):
            await N.asay(publish, line, op_id=f"fea:plan:{n}:{i}")
    elif levers:
        await N.asay(publish, N.retry_line(n, levers[-1]), op_id=f"fea:retry:{n}")
    await N.astep(publish, "machine", N.machine_words(machine), op_id=f"fea:machine:{n}")
    meshing = getattr(publish, "ameshing", None)
    if meshing is not None:
        await meshing(ENGINE, budget_s, None, op_id=f"meshing:fea:{n}")


async def _relayed_mesh(publish, ws: Path, *, seq: int, timeout, of: int | None = None):
    """One run of the mesher, with its stage reports (the remote beacon, read by the mesh client)
    relayed to the run's live line while it runs - in the words contracts/narration gives them."""
    from meshpipeline.contracts import mesh_progress
    from meshpipeline.engines.live_estimate import planned_live_count
    from meshpipeline.engines.runtime import get_engine

    # THE NOUN IS THE ENGINE'S OWN (EngineSpec.counted_as): this path delivers an FEA deck, whose
    # count is in ELEMENTS - said whether or not the plan has an estimate to count them against.
    _unit = planned_live_count(ENGINE, ws)[1]
    sink = None if publish is None or getattr(publish, "amesh_stage", None) is None else \
        mesh_progress.publishing_sink(publish, asyncio.get_running_loop(),
                                      op_prefix=mesh_progress.stage_op_prefix(ws, seq),
                                      of=of, unit=_unit)
    with mesh_progress.relaying(sink):
        return await asyncio.to_thread(get_engine(ENGINE).run_cartesian_mesh, ws, timeout=timeout)


def _refuse(ws: Path, *, cause: str, text: str, gate: str = "fea_plan", **facts) -> None:
    from meshpipeline.engines.preflight import PreflightRefusal

    base = {"engine": "gmsh", "deterministic": True}
    if gate == "fea_plan":
        base["before_meshing"] = True
    PreflightRefusal(gate=gate, cause=cause, builder_text=f"[{gate.upper()}] {text}",
                     facts={**base, **facts}).write(ws)


#: The gate an exhausted ladder is reported under (the FEA quality floor it could not meet).
QUALITY_GATE = "fea_quality"


def ladder_facts(ws: Path, passes: list[dict], levers: list[dict]) -> dict:
    """What the user's sentence and the retry policy read when the ladder ran out: that it is
    deterministic and exhausted, how many passes and which levers, and where the worst elements
    are (millimetres) with how many sit under the floor."""
    q = _read_quality(ws)
    fq = (q.get("fea") or {}).get("quality") or {}
    where = (fq.get("worst_at_m") or {}).get("scaled_jacobian")
    facts: dict = {"fea": True, "ladder_exhausted": True, "passes": len(passes),
                   "out_of_memory": bool(passes and passes[-1].get("out_of_memory")),
                   "estimate": int((passes[-1].get("estimate") or 0) if passes else 0),
                   "levers": [lv["lever"] for lv in levers],
                   "under_floor": int(q.get("elements_under_floor") or 0),
                   "elements": int(q.get("cells") or 0),
                   # NO PASS BUILT A MESH (the fill refused the surface meshes): said as that, with the
                   # surfaces it named - never as badly shaped elements (car_vs_pole, 24 Oct)
                   "built_none": bool(passes) and not any(p.get("built") for p in passes)}
    named = sorted({int(t) for p in passes for t in p.get("bad_surfaces") or []})
    if named:
        facts["bad_surfaces"] = named
    if isinstance(where, list) and len(where) == 3:
        facts["worst_at_mm"] = [round(float(v) * 1000.0, 1) for v in where]
    if (q.get("bad_spread_mm")) is not None:
        facts["spread_mm"] = q["bad_spread_mm"]
    return facts


def _killed(result: dict) -> bool:
    """The mesher was killed by the kernel (SIGKILL: its machine's memory limit) - said as the signal, or
    as exit 137 / -9 when the container reports it so."""
    return (str(result.get("signal") or "") == "SIGKILL" or "killed by SIGKILL" in str(result.get("log_tail") or "")
            or result.get("rc") in (137, -9))


def _out_of_memory(result: dict) -> bool:
    """Killed for memory, with nothing built."""
    return bool(_killed(result) and result.get("rc") not in (0, 4))


def _peak_gib(result: dict) -> float | None:
    """The mesher's peak memory when the run measured one (quality.json's peak_rss_mb), in GiB."""
    q = result.get("remote_quality") or result.get("quality") or {}
    try:
        mb = float((q or {}).get("peak_rss_mb") or 0.0)
    except (TypeError, ValueError):
        return None
    return mb / 1024.0 if mb > 0 else None


#: What a failed fill of FUSED parts says (gmsh's own words): the fused boundary is not one clean solid.
FUSED_FILL_ERRORS = ("PLC Error", "intersect", "overlapping facets", "BRep contains more volumes",
                     "Invalid boundary mesh",
                     # the fused faces' own edges do not close (kart_chassis, 25 Oct: two passes of 450 s
                     # each, "The 1D mesh seems not to be forming a closed loop", before the tie passed)
                     "not to be forming a closed loop")


def _fused_fill_failed(result: dict, facts: dict) -> bool:
    """The fill failed on the fused parts' own boundary (nothing built): a volume left empty, or a
    boundary that crosses itself - not a matter of sizes or algorithm."""
    log = str(result.get("log_tail") or "")
    if facts.get("built"):
        return False
    if _one_part_faces(facts.get("bad_faces") or []):
        # THE FILL NAMED FACES OF ONE PART ONLY (libredrive_pulley_housing, 25 Oct: two tangent faces of the
        # housing, "overlapping facets", no joint near): the part's own surface mesh, which the tie would
        # mesh the same way - the ladder's levers (MeshAdapt there, LAD-fea-6) come first
        return False
    return _empty_volumes_named(log) or any(w in log for w in FUSED_FILL_ERRORS)


def _one_part_faces(names: list) -> bool:
    """Every face named is one part's own (Part_k_Face_j, the same k)."""
    from meshpipeline.cad.fea_assembly import part_of_face

    parts = {part_of_face(str(n)) for n in names}
    return len(parts) == 1 and None not in parts


#: The share of an invalid fused pass's bad surfaces that must be fused interfaces for the tie to come
#: at once: the bad elements are where the parts were joined, not in the parts.
FUSED_BAD_SHARE = 0.5


def _fused_joints_invalid(q: dict) -> bool:
    """A FUSED BONDED PASS THAT CAME BACK INVALID AT THE JOINTS (kart_roll_cage, 24 Oct: welded tubes
    overlapping at every joint - 1,495 s and then an AI-lever retry of 3,700 s, both with inverted
    elements on the fused interfaces, before the tie passed in 2.4 min): most of its bad surfaces are
    fused interfaces, so no other setting of the same fused mesh will do, and the tie comes now."""
    bad = list(q.get("fea_bad_surfaces") or [])
    on = list(q.get("fea_bad_interface") or [])
    return bool(bad and on and len(on) >= FUSED_BAD_SHARE * len(bad))


def _keep_pass_log(ws: Path, n: int) -> None:
    """Every pass keeps its own gmsh log, its quality record and its failure note in its pass folder - not
    only the last (where a pass went wrong is read afterwards, pass by pass)."""
    d = ws / PASS_FOLDER.format(n=n)
    d.mkdir(exist_ok=True)
    for name in ("gmshfea.log", "quality.json", "fea_failure.json"):
        if (ws / name).exists():
            shutil.copy2(ws / name, d / name)


def _empty_volumes_named(log: str) -> bool:
    from meshpipeline.engines.gmsh.fea_adapter import EMPTY_VOLUMES
    return EMPTY_VOLUMES in log


def ladder_words(passes: list[dict], levers: list[dict]) -> str:
    """The builder-facing line for an exhausted ladder (the user's sentence is failure_cause's)."""
    tried = ", ".join(lv["lever"] for lv in levers) or "no lever"
    if passes and passes[-1].get("out_of_memory"):
        # LAD-fea-7
        return (f"the mesher ran out of memory building about {int(passes[-1].get('estimate') or 0):,} elements "
                f"({len(passes)} pass(es); levers {tried}) - this plan needs more memory than the mesh job has; "
                "another attempt on the same machine runs out the same way.")
    if passes and not any(p.get("built") for p in passes):
        named = sorted({int(t) for p in passes for t in p.get("bad_surfaces") or []})
        return (f"no pass of the FEA plan built a mesh - the fill could not start ({len(passes)} pass(es); levers "
                f"{tried}; surfaces it named {named or 'none'}). The plan is deterministic: another attempt "
                "fails the same way." + _rung_said(passes))
    readings = [float(p["min_sicn"]) for p in passes if p.get("min_sicn") is not None]
    worst = min(readings) if readings else None
    return (f"no pass of the FEA plan built a valid mesh - a degenerate or inverted element ({len(passes)} "
            f"pass(es); levers {tried}; best worst element {worst}). The plan is deterministic: another attempt "
            "builds the same mesh." + _rung_said(passes))


def _rung_said(passes: list[dict]) -> str:
    """LAD-fea-9 in the exhausted ladder's line: what the last rung (fTetWild) said when it failed too."""
    rung = [p for p in passes if p.get("engine") == "ftetwild"]
    if not rung:
        return ""
    return f" fTetWild (the last rung) built no valid mesh either: {rung[-1].get('rung_error') or 'no mesh'}."


def _read_quality(ws: Path) -> dict:
    try:
        return json.loads((ws / "quality.json").read_text())
    except (OSError, ValueError):
        return {}


#: The pass files linked rather than copied: the mesh and its solver files, which no step writes into
#: once made - a later pass clears them (unlinks) before it meshes, and its result lands by replacing
#: names (sandbox/safe_extract moves over). The records beside them (the plan, the quality) are
#: rewritten in place by the next pass, so those are always copied.
LINKED = ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.msh")


def _same_file(src: Path, dest: Path) -> None:
    """`dest` becomes `src`'s content: a hard link for the mesh files (no bytes copied - a 34 M-element
    pass is about 37 GB of them, and the worker's disk then holds a pass and its copy once), else a
    copy."""
    dest.unlink(missing_ok=True)
    if src.name in LINKED:
        try:
            os.link(src, dest)
            return
        except OSError:
            pass
    shutil.copy2(src, dest)


def _keep_pass(ws: Path, n: int) -> None:
    d = ws / PASS_FOLDER.format(n=n)
    d.mkdir(exist_ok=True)
    # with the record of what this pass kept in the bucket and its stand-ins (RUN-7): a pass
    # restored later delivers its own kept files, never the last pass's
    for name in DELIVERABLES + BF.STAND_INS:
        if (ws / name).exists():
            _same_file(ws / name, d / name)


def _restore_pass(ws: Path, n: int) -> None:
    d = ws / PASS_FOLDER.format(n=n)
    for name in DELIVERABLES + BF.STAND_INS:
        if (d / name).exists():
            _same_file(d / name, ws / name)
        else:
            (ws / name).unlink(missing_ok=True)      # the delivered pass did not write it


def _read_failure(ws: Path) -> dict:
    try:
        return json.loads((ws / "fea_failure.json").read_text())
    except (OSError, ValueError):
        return {}


def _clear_outputs(ws: Path) -> None:
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.msh", "quality.json", "fea_failure.json"):
        (ws / name).unlink(missing_ok=True)
    BF.clear(ws)            # the last pass's record of kept files and its stand-ins (RUN-7)


async def _native(ws: Path, machine: dict, work: dict, job_id: str, n: int, run,
                  publish=None, of: int | None = None) -> tuple[dict, dict, dict]:
    """One native pass on `machine`: RUN-3 (out of memory: one machine up) and RUN-5 (still working at
    the Cloud Run wall: a VM) as every pass. Returns (the result, the machine it ran on, its work).
    `of`: the elements THIS pass's plan estimates, for the live line's "N of about M" - the plan's
    own number or nothing."""
    from meshpipeline.contracts import mesh_machine as MM

    while True:
        result = await _relayed_mesh(publish, ws, seq=900 + n, timeout=work["ceiling_s"], of=of)
        if _out_of_memory(result):
            moved, rule, decided = MM.step_up(machine, measured_gib=_peak_gib(result)), "RUN-3", "step up"
        elif result.get("timed_out") and not result.get("stalled") and not work.get("budget"):
            # (a pass its own budget stopped - LAD-fea-18's rung - is not moved: it would only grind on elsewhere)
            moved, rule, decided = MM.off_the_wall(machine), "RUN-5", "off the wall"
        else:
            return result, machine, work
        if moved is None:
            return result, machine, work
        machine = moved
        MM.write(ws, machine)
        work = {**work, "ceiling_s": int(min(ceiling_for(run, machine), work.get("budget_ceiling_s") or math.inf))}
        _cite(job_id, [{"stage": "fea_machine", "decided": decided, "value": machine["machine"], "rule": rule,
                        "inputs": {"pass": n, "from": machine.get("stepped_from"), "need_gib": machine["need_gib"]},
                        "words": machine["words"]}])
        _clear_outputs(ws)


def not_run(result: dict) -> bool:
    """RUN-4: the pass never ran on OUR side - the mesh job or its VM could not be started (a refused
    permission, a quota), the run was given up, or its result never came back. The FEA mesher has no
    exit code of its own for this (contracts/mesh_execution.RC_INFRASTRUCTURE), so it is never read as
    the part's verdict: no lever is pulled, and the attempt ends as ours (job bf863215, 26 Oct: five
    passes and the last rung each refused 'HTTP 403' by the VM route, then told as the CAD's fault)."""
    from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE
    # a mesh BUILT but too big for this worker to collect is its own outcome (TOO_BIG: the result is kept)
    return (isinstance(result, dict) and result.get("rc") == RC_INFRASTRUCTURE and not result.get("timed_out")
            and not result.get("too_big_for_worker"))


def not_run_words(unrun: dict) -> str:
    """What a pass our side never ran says, for the why record and the refusal."""
    where = unrun.get("machine") or "the mesh job"
    return (f"pass {unrun.get('pass')} could not be run on {where} on our side "
            f"({unrun.get('error') or 'no reason given'})")


def _rung_error(result: dict) -> str:
    """What the rung's pass said when it failed: the adapter's own fTetWild line, else the log's last line."""
    tail = [ln.strip() for ln in str(result.get("log_tail") or "").splitlines() if ln.strip()]
    said = [ln for ln in tail if "fTetWild could not" in ln or "fTetWild is not installed" in ln] or tail
    return (said[-1] if said else f"exit {result.get('rc')}")[:400]


async def _last_rung(ws: Path, record: dict, diag_m: float, first_plan: dict, passes: list[dict], *,
                     job_id: str, run, publish=None, crossing: dict | None = None) -> tuple[dict, list[dict]]:
    """LAD-fea-9: at most fea_tetwild.MAX_RUNG_PASSES passes - an assembly's parts the failed passes
    named first, then every part - each on the plan the last round of gmsh passes started from (the other
    parts meshed as that pass meshed them), plus fTetWild's settings (engines/fea_tetwild.params).
    Returns (the record, the rung's passes)."""
    import copy

    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload
    from meshpipeline.engines import fea_tetwild as TW

    if (record.get("assembly_plan") or {}).get("mode") == "bonded":
        # the rung meshes a part on its own: a fused assembly is staged apart and tied first (PLAN-fea-17)
        record = await asyncio.to_thread(restage_tied, ws, record)
    gm = [p for p in passes if p.get("engine") != TW.ENGINE]
    ran: list[dict] = []
    for rung in range(1, TW.MAX_RUNG_PASSES + 1):
        parts = TW.rung_parts(record, gm, rung)
        n = len(passes) + len(ran) + 1
        plan = copy.deepcopy(first_plan)
        if record.get("assembly_plan"):
            plan["assembly"] = dict(record["assembly_plan"])          # as staged now (PLAN-fea-17)
        # LAD-fea-17: the rung's own sizes (the envelope from the real walls - never a crossing face's reading)
        settings = TW.params(plan, record, diag_m, parts, crossing=crossing)
        # RUN-6: gmsh's model for the parts gmsh holds, fTetWild's own measured model for the parts it builds
        elements = float((plan.get("estimate") or {}).get("elements") or 0)
        process_gib, files_gib = memory_need(elements)
        machine = TW.rung_machine(elements, process_gib, files_gib, margin=MEMORY_MARGIN)
        # LAD-fea-18: THE RUNG'S TIME, sized from the part (its estimate at fTetWild's measured rate on the
        # machine's cores), within the machine's wall; the pass's own backstop a margin past it
        timed = TW.rung_budget(elements, cpus=int(machine.get("cpus") or TW.CLOUD_RUN_CPUS),
                               wall_s=ceiling_for(run, machine))
        settings["budget_s"] = timed["budget_s"]
        plan["mesh"] = {**(plan.get("mesh") or {}), "engine": TW.ENGINE, "ftetwild": settings}
        said = L.rung_words(parts, rung, crossing=crossing)
        plan["words"] = [*(plan.get("words") or []), said]
        _cite(job_id, [{"stage": "fea_ladder", "decided": "last rung", "value": TW.ENGINE, "rule": "LAD-fea-9",
                        "inputs": {"pass": n, "rung": rung, "parts": parts, "per_part": settings["per_part"],
                                   "version": settings["version"], "threads": settings["threads"]},
                        "words": said}])
        _cite(job_id, [{"stage": "fea_ladder", "decided": "rung sizes", "value": TW.SIZE_RULE, "rule": TW.SIZE_RULE,
                        "inputs": {"pass": n, "per_part": settings["per_part"], "basis": settings["basis"]},
                        "words": TW.size_words(settings)}])
        _cite(job_id, [{"stage": "fea_time", "decided": "rung budget", "value": timed["budget_s"],
                        "rule": TW.TIME_RULE, "inputs": {"pass": n, **timed}, "words": TW.budget_words(timed)}])
        write_plan(ws, plan)
        run.note_authoring()
        _clear_outputs(ws)
        note_native_pass(ws, n)
        note_native_payload(ws, [p for p in PAYLOAD if (ws / p).exists()])
        _cite(job_id, [{"stage": "fea_machine", "decided": machine["where"], "value": machine["machine"],
                        "rule": TW.MEMORY_RULE, "inputs": {"pass": n, **machine["ftetwild"]},
                        "words": machine["words"]}])
        MM.write(ws, machine)
        work = planned_work(plan, ceiling_s=min(ceiling_for(run, machine), timed["ceiling_s"]))
        work.update(budget=timed["ceiling_s"] < ceiling_for(run, machine), budget_ceiling_s=timed["ceiling_s"])
        await run.fence("start native mesh")
        t0 = time.monotonic()
        from meshpipeline.contracts import narration as _N
        await _N.asay(publish, "The usual mesher could not meet every check, so a second mesher tries this part.",
                      op_id=f"fea:rung:{n}")
        await _N.astep(publish, "machine", _N.machine_words(machine), op_id=f"fea:rung:machine:{n}")
        result, machine, work = await _native(ws, machine, work, job_id, n, run, publish=publish,
                                              of=int(elements) or None)
        await run.fence("accept native mesh output")
        q = _read_quality(ws) if result.get("rc") in (0, 4) else {}
        made = bool(q) and BF.present(ws, "mesh.inp")
        run.note_native_run(produced_usable_mesh=made and result.get("rc") == 0)
        facts = {**L.pass_facts(n, result.get("rc"), q, plan, log=str(result.get("log_tail") or ""),
                                killed=_killed(result)),
                 "native_s": round(time.monotonic() - t0, 1), "engine": TW.ENGINE, "rung": rung, "parts": parts,
                 "ran_on": {k: machine.get(k) for k in ("where", "machine", "memory_gib", "why")}}
        if result.get("timed_out"):
            facts["timed_out"] = True
        if not facts["valid"]:
            facts["rung_error"] = _rung_error(result)
        if not_run(result):
            # RUN-4: our side never ran it - no further rung pass would run either
            facts["not_run"] = True
            ran.append(facts)
            break
        ran.append(facts)
        _keep_pass_log(ws, n)
        _cite(job_id, [{"stage": "fea_ladder", "decided": "last rung pass", "value": bool(facts["valid"]),
                        "rule": "LAD-fea-9", "inputs": {"pass": n, "rung": rung, "cells": facts["cells"],
                                                        "min_sicn": facts["min_sicn"],
                                                        "error": facts.get("rung_error")},
                        "words": (f"fTetWild built a valid mesh on pass {n}." if facts["valid"] else
                                  f"fTetWild's pass {n} built no valid mesh ({facts.get('rung_error')}).")}])
        if made:
            _keep_pass(ws, n)
        # LAD-fea-18: a rung pass out of its time is not followed by another (it would only spend more)
        if (facts["valid"] or parts is None or result.get("timed_out")
                or TW.OUT_OF_TIME in str(facts.get("rung_error") or "")):
            break
    if not any(f["valid"] for f in ran) and gm and (ws / PASS_FOLDER.format(n=gm[-1]["pass"])).exists():
        # the rung built nothing valid either: the workspace is the last gmsh pass's again, as it was
        # before the rung, so the exhausted ladder is told from that pass's measures
        _restore_pass(ws, int(gm[-1]["pass"]))
    return record, ran


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun, source_path: str = ""):
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload

    ws = Path(workspace)
    if staged_solid(ws) is None:
        logger.warning("fea driver: no staged solid in %s - job_id=%s", ws, job_id)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=NO_SOURCE)
        return False, NO_SOURCE, outcome

    try:
        made = await asyncio.to_thread(solids_from_shells, ws)
        if made:
            _cite(job_id, [{"stage": "fea_plan", "decided": "closed shells read as solids", "value": made,
                            "rule": "MESH-1", "inputs": {"solids": made},
                            "words": f"The file's {made} closed shell(s) were read as the solid(s) they bound."}])
    except Exception as exc:  # noqa: BLE001 - the part is meshed, or refused, as it stands
        logger.warning("fea driver: closed shells not read as solids (%s)", type(exc).__name__)
    # MESH-8 FOR A SINGLE PART: its skins left out, its faces still named for the CAD faces they came from
    skins = await asyncio.to_thread(open_single_skins, ws)
    from meshpipeline.contracts import narration as _N
    await _N.astep(publish, "measure", "Measuring your part", op_id="fea:measure")
    try:
        record, surface, diag_m = await asyncio.to_thread(measure_staged, ws)
        if skins and skins.get("origin"):
            record = named_as_cad(record, surface, skins)
    except Exception as exc:  # noqa: BLE001 - recorded as the attempt's reason
        logger.exception("fea driver: the staged solid could not be measured - job_id=%s", job_id)
        _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=f"the part could not be measured ({exc})",
                reason=f"the part could not be measured ({type(exc).__name__})")
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=BUILD_FAILED)
        return False, BUILD_FAILED, outcome

    if skins and skins.get("origin"):
        _cite(job_id, [{"stage": "fea_plan", "decided": "skins left out", "rule": "MESH-8",
                        "value": sum(len(sk["faces"]) for sk in skins["skins"]), "inputs": {"skins": skins["skins"]},
                        "words": " ".join(skin_words(skins["skins"]))}])
    elif skins and skins.get("note"):
        _cite(job_id, [{"stage": "fea_plan", "decided": "skins kept", "rule": "MESH-8", "value": 0,
                        "inputs": {"note": skins["note"]},
                        "words": f"The part has skins of no thickness, but {skins['note']}: it is meshed as drawn."}])
    staged = staged_solid(ws)
    healed = any("healed" in str(n) for n in record.get("notes") or [])
    _cite(job_id, [{"stage": "fea_plan", "decided": "staged solid", "value": staged.name if staged else None,
                    "rule": "PLAN-fea-15", "inputs": {"healed": healed},
                    "words": (f"The part was read from {staged.name if staged else 'the staged file'}"
                              + (", healed before it was measured." if healed else "."))}])
    if int(record.get("bodies") or 1) > 1:
        # PLAN-fea-17: AN ASSEMBLY - its parts staged as the user said they are joined (bonded by
        # default), every face Part_k_Face_j, each part sized by its own rules
        connection = connection_of(state)
        try:
            record = await asyncio.to_thread(prepare_assembly, ws, record, surface, connection=connection,
                                             source_path=source_path, gap_joins=gap_joins_of(state),
                                             pairs=pairs_of(state))
        except Exception as exc:  # noqa: BLE001 - recorded as the attempt's reason
            logger.exception("fea driver: the assembly could not be staged - job_id=%s", job_id)
            _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=f"the assembly could not be staged ({exc})",
                    reason=f"the assembly could not be staged ({type(exc).__name__})")
            await run.fence("deliver builder outcome")
            outcome = run.outcome(produced_deliverable=False, failure_marker=BUILD_FAILED)
            return False, BUILD_FAILED, outcome
        asm = record.get("assembly_plan") or {}
        joins = asm.get("joins") or {}
        if joins:
            _cite(job_id, [{"stage": "fea_plan", "decided": "joints", "value": joins.get("found_by"),
                            "rule": "PLAN-fea-18",
                            "inputs": {"findings": len(joins.get("findings") or []), "gaps": joins.get("gaps"),
                                       "skins": joins.get("skins"), "gap_joins": gap_joins_of(state)},
                            "words": " ".join(str(n) for n in joins.get("notes") or [])
                            or "Where the parts meet was measured on their surfaces."}])
        if asm.get("skins"):
            _cite(job_id, [{"stage": "fea_plan", "decided": "skins left out", "rule": "MESH-8",
                            "value": sum(len(sk["faces"]) for sk in asm["skins"]),
                            "inputs": {"skins": asm["skins"]},
                            "words": " ".join(skin_words(asm["skins"]))}])
        _cite(job_id, [{"stage": "fea_plan", "decided": "assembly", "value": asm.get("mode"),
                        "rule": "PLAN-fea-17",
                        "inputs": {"bodies": record.get("bodies"), "connection": connection,
                                   "pairs": asm.get("pairs"), "interfaces": asm.get("interfaces"),
                                   "tie_first": asm.get("tie_first")},
                        "words": " ".join(P.assembly_words(asm)[:2])}])
        # PLAN-fea-19/20: each pair's type and each overlap's owner, with its reason
        from meshpipeline.cad import fea_pairs as FP

        _cite(job_id, FP.decisions(list(asm.get("joints") or []), connection=connection,
                                   gap_joins=gap_joins_of(state)))
    if not record.get("faces") or not any("thickness_min_mm" in (r or {}) for r in record["faces"].values()):
        # PLAN-fea-3 cannot be planned on walls nobody measured: stop before meshing rather than
        # deliver a mesh whose walls were never held to the rule
        why = "the part's walls could not be measured, so the elements through them cannot be planned"
        _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=why, reason=why)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=BUILD_FAILED)
        return False, BUILD_FAILED, outcome

    # AI GOVERNS, RULES GUARANTEE (engines/fea_ai): the judgement calls for this part and the user's
    # words, asked once and checked; the rules make them exact. No answer: the rules' own plan. (The
    # limit it is shown is the fixed one, never the clock's, so the same part asks the same question
    # and the decision cache answers it the same way.)
    ai = await FA.decide(record, state or {}, P.zones(record), diag_m=diag_m,
                         limit=element_limit(record)[0], job_id=job_id, run=run, wait_s=ai_wait_s(run))
    # what the AI did with the user's notes, kept for the result's "From your notes" line
    if (ai.get("decision") or {}).get("notes_remark"):
        from meshpipeline.contracts.user_notes import record_remark
        record_remark(ws, ai["decision"]["notes_remark"], by="fea_ai")
    # PLAN-fea-27: an element count the user named, as the AI read it from their notes (AI-fea-4)
    target = element_target(state, ai)
    # SHSTAGE-6: MESH AS: SHELL - the shell mesh and its solver files first, from the staged solid as read (the
    # assembly staging above never rewrites it); the solid deck is made as always beside it. SHMESH-23: once the
    # user's element count is read, so the shell's sizes follow it as the solid's do.
    await shell_pass(ws, state, job_id=job_id, run=run, target=target)
    # AI-fea-9 / PLAN-fea-29: the element sizes the user asked for, part by part, tied to the parts by the vision
    # model from the parts' facts and pictures, checked by the rules; nothing applied when it cannot be read
    part_sizes = await PS.decide(ws, record, surface, state or {}, job_id=job_id, run=run, wait_s=ai_wait_s(run))
    sizes = PS.sizes_of(part_sizes)
    if part_sizes.get("status") != "none":
        _cite(job_id, PS.decisions(part_sizes))
    # A COUNT OR SIZES THE USER ASKED FOR NEVER FAIL THE RUN: when no pass builds a valid mesh of the plan their
    # choices made, the rules' own plan is meshed instead, and said (PLAN-fea-29)
    by_choice_failed = ""
    # PLAN-fea-31: the count the user asked for is checked on the BUILT mesh; off by more than REAIM_SHARE, the plan
    # is solved again once with the measured built-over-planned and built again
    correction: float | None = None
    reaim = False
    levers: list[dict] = []
    passes: list[dict] = []
    crossing_found: dict | None = None
    tripped: dict | None = None
    too_big: dict | None = None
    unrun: dict | None = None
    rate, cls = fill_rate(record)
    chosen: dict = {}
    first_plan: dict = {}
    n0 = 0
    while True:
        for n in range(n0 + 1, n0 + L.MAX_PASSES + 1):
            await _N.astep(publish, "plan", f"Planning the mesh · {_N.pass_words(n - n0, L.MAX_PASSES)}"
                           if n > n0 + 1 else "Planning the mesh", op_id=f"fea:planning:{n}")
            if n == n0 + 1:
                # PLAN-fea-8: the rules' own plan, coarsened to fit under the limit when it must - never a
                # stop before meshing
                plan = await asyncio.to_thread(choose_plan, record, surface, diag_m, levers=levers,
                                               ceiling_s=ceiling_for(run), ai=ai, target=target, sizes=sizes or None,
                                               correction=correction)
                plan["part_sizes"] = part_sizes
                if by_choice_failed:
                    plan["by_choice_failed"] = by_choice_failed
                plan = _restate(record, plan)
                chosen = {"limit": plan["limit"], "zone_floor_m": plan.get("zone_floor_m"),
                          "zone_floors": plan.get("zone_floors_m"), "coarsened": plan.get("coarsened"),
                          # PLAN-fea-27: every later pass keeps the factor the user's count gave
                          "scale": plan.get("scale"), "beyond": plan.get("beyond"), "target": plan.get("target"),
                          "sizes": sizes or None, "by_choice_failed": by_choice_failed,
                          "target_missed": plan.get("target_missed"), "target_unread": plan.get("target_unread")}
                first_plan = plan
                if P.target_remark(plan):
                    # PLAN-fea-27: what was done with the count the notes named, for the result's line
                    from meshpipeline.contracts.user_notes import record_remark
                    record_remark(ws, P.target_remark(plan), by="fea_target")
                # every later pass plans with the decision the first one kept (refinements left out
                # when they would not fit stay out)
                if plan.get("ai"):
                    ai = {k: v for k, v in plan["ai"].items() if k != "applied"}
            else:
                floor_m = chosen["zone_floor_m"]
                if L.coarse_factor(levers) < 1.0:
                    # LAD-fea-4: the built mesh was over the limit - the finest zones coarser, from the
                    # floor already in force (or the finest zone), never past their validity minimums
                    sized = first_plan.get("zones") if chosen.get("scale") else P.zones(record)
                    start = floor_m or min([float(z["size_m"]) for z in sized or []] or [diag_m / P.MIN_DIVISIONS])
                    floor_m = start / L.coarse_factor(levers)
                plan = await asyncio.to_thread(make_plan, record, surface, diag_m, levers=levers,
                                               limit=int(chosen["limit"]["elements"]), zone_floor_m=floor_m, ai=ai,
                                               zone_floors=chosen.get("zone_floors"), scale=chosen.get("scale"),
                                               beyond=chosen.get("beyond"), sizes=chosen.get("sizes"))
                plan["limit"] = chosen["limit"]
                plan["part_sizes"] = part_sizes
                if chosen.get("by_choice_failed"):
                    plan["by_choice_failed"] = chosen["by_choice_failed"]
                for key in ("target", "target_missed", "target_unread"):
                    if chosen.get(key):
                        plan[key] = chosen[key]
                if chosen.get("coarsened"):
                    plan["coarsened"] = chosen["coarsened"]
                    plan["fits"] = True
                plan = _restate(record, plan)
            if n == n0 + 1:
                _cite(job_id, plan["decisions"])
            write_plan(ws, plan)
            run.note_authoring()
            _clear_outputs(ws)
            note_native_pass(ws, n)
            note_native_payload(ws, [p for p in PAYLOAD if (ws / p).exists()])
            # WHERE IT RUNS (RUN-1..3): the Cloud Run mesh job when the plan fits it, else a VM sized to
            # fit - decided now, from the memory model, so a big plan never wastes a first run
            from meshpipeline.contracts import mesh_machine as MM
            process_gib, files_gib = memory_need(float((plan.get("estimate") or {}).get("elements") or 0))
            machine = MM.choose(process_gib=process_gib, workspace_gib=files_gib, why="memory model")
            _cite(job_id, [{"stage": "fea_machine", "decided": machine["where"], "value": machine["machine"],
                            "rule": "RUN-2" if machine.get("over_ceiling") else "RUN-1", "inputs": {k: machine[k] for k in ("need_gib", "process_gib",
                                                                                 "workspace_gib", "memory_gib")}
                            | {"pass": n}, "words": machine["words"]}])
            # RUN-5: AND BY TIME - a plan planned to pass the Cloud Run job's wall runs on a VM from the
            # start, so it is never stopped by a limit it would only meet because of where it ran
            timed = MM.for_time(machine, planned_s=planned_seconds(plan, rate), wall_s=ceiling_for(run))
            if timed is not machine:
                machine = timed
                _cite(job_id, [{"stage": "fea_machine", "decided": "gce", "value": machine["machine"],
                                "rule": "RUN-5", "inputs": {"pass": n, "planned_s": planned_seconds(plan, rate),
                                                            "wall_s": ceiling_for(run)},
                                "words": machine["words"]}])
            MM.write(ws, machine)
            # PLAN-fea-13 / LAD-fea-15: watched for progress, and given the time its estimate allows (BUDGET_FACTOR
            # times its planned time, at least BUDGET_FLOOR_S, at most the machine's wall)
            work = pass_work(plan, rate=rate, cls=cls, machine=machine)
            _cite(job_id, [{"stage": "fea_time", "decided": "watchdog", "value": work["stall_window_s"],
                            "rule": "PLAN-fea-13",
                            "inputs": {k: work[k] for k in ("amount", "rate", "class", "planned_s", "ceiling_s",
                                                             "stall_window_s")} | {"pass": n},
                            "words": (f"Pass {n}: about {int(work['amount'] or 0):,} elements. It is stopped if it makes "
                                      f"no progress for {work['stall_window_s'] // 60} minutes, or after "
                                      f"{work['ceiling_s'] // 60} minutes"
                                      + (" - the time its size allows; past that the next way to mesh it is tried."
                                         if work.get("budget") else ", the hard limit of the machine."))}])
            await run.fence("start native mesh")
            # THE LIVE LINE (contracts/narration): what was decided and why, where it runs, and the
            # mesher's own stages as it reports them. This path used to say nothing here, so a run
            # meshing for an hour on a VM still read "Planning the mesh..." on the page.
            await _narrate_pass(publish, n=n, first=n == n0 + 1, plan=plan, levers=levers,
                                machine=machine, budget_s=int(work["ceiling_s"]))
            native_t0 = time.monotonic()
            runs_here = 0
            while True:
                runs_here += 1
                result = await _relayed_mesh(publish, ws, seq=n * 10 + runs_here, timeout=work["ceiling_s"],
                                             of=int((plan.get("estimate") or {}).get("elements") or 0) or None)
                if _out_of_memory(result):
                    # RUN-3: OUT OF MEMORY -> THE SAME PLAN ONE MACHINE UP (no lever, no coarsening, no
                    # re-plan), sized from the peak seen when there is one, up to the largest machine
                    moved, rule, decided = MM.step_up(machine, measured_gib=_peak_gib(result)), "RUN-3", "step up"
                elif result.get("timed_out") and not result.get("stalled") and not work.get("budget"):
                    # RUN-5: STILL WORKING AT THE CLOUD RUN WALL -> THE SAME PLAN ON A VM, which has no
                    # wall (nothing coarsened, no lever); on a VM already there is nowhere to move. A pass
                    # stopped by its own budget (LAD-fea-15) is not moved: it would only grind on elsewhere
                    moved, rule, decided = MM.off_the_wall(machine), "RUN-5", "off the wall"
                else:
                    break
                if moved is None:
                    break
                machine = moved
                MM.write(ws, machine)
                work = pass_work(plan, rate=rate, cls=cls, machine=machine)
                _cite(job_id, [{"stage": "fea_machine", "decided": decided, "value": machine["machine"],
                                "rule": rule, "inputs": {"pass": n, "from": machine.get("stepped_from"),
                                                         "need_gib": machine["need_gib"]},
                                "words": machine["words"]}])
                _clear_outputs(ws)
            native_s = round(time.monotonic() - native_t0, 1)
            await run.fence("accept native mesh output")
            if not_run(result):
                # RUN-4: OUR SIDE NEVER RAN THIS PASS (its machine could not be started, its result never
                # came back): not the part's verdict - no lever, no smaller plan; the attempt ends as ours
                run.note_native_run(produced_usable_mesh=False)
                unrun = {"pass": n, "error": _rung_error(result), "machine": machine.get("machine"),
                         "where": machine.get("where")}
                passes.append({**L.pass_facts(n, result.get("rc"), {}, plan), "native_s": native_s, "not_run": True,
                               "ran_on": {k: machine.get(k) for k in ("where", "machine", "memory_gib", "why")}})
                _cite(job_id, [{"stage": "fea_machine", "decided": "not run", "value": machine.get("where"),
                                "rule": "RUN-4", "inputs": unrun, "words": not_run_words(unrun).capitalize() + "."}])
                break
            # LAD-fea-15: THE PASS'S OWN BUDGET RAN OUT (the time its estimate allows, not the machine's wall):
            # the part's slowness, not ours - the pass is a fill that failed, and the ladder goes on (a surface
            # stage that ran over: LAD-fea-8 first; then the other 3D algorithm; at the end the last rung)
            spent = bool(result.get("timed_out") and not result.get("stalled") and work.get("budget"))
            stuck_on = (L.stalled_surface(str(result.get("log_tail") or ""))
                        if result.get("stalled") or spent else None)
            stall_lever = L.surface_stall_lever(stuck_on, levers, n=n) if stuck_on is not None else None
            if spent:
                _cite(job_id, [{"stage": "fea_time", "decided": "budget spent", "value": work["ceiling_s"],
                                "rule": "LAD-fea-15", "inputs": {"pass": n, "planned_s": work.get("planned_s"),
                                                                 "budget_s": work["ceiling_s"], "surface": stuck_on},
                                "words": budget_words(work, n, stuck_on)}])
            if result.get("timed_out") and stall_lever is not None:
                # LAD-fea-8: A SURFACE STAGE THAT STALLED is the part's, not ours - gmsh's own switch to
                # MeshAdapt on one face ran past the watchdog - so the next pass meshes the surfaces without it
                run.note_native_run(produced_usable_mesh=False)
                passes.append({**L.pass_facts(n, result.get("rc"), {}, plan, log=str(result.get("log_tail") or "")),
                               "native_s": native_s, "stalled_surface": stuck_on,
                               "ran_on": {k: machine.get(k) for k in ("where", "machine", "memory_gib", "why")}})
                _cite(job_id, [{"stage": "fea_ladder", "decided": "lever", "value": stall_lever["lever"],
                                "rule": stall_lever["lever"], "inputs": stall_lever["inputs"],
                                "words": stall_lever["words"]}])
                levers.append(stall_lever)
                continue
            if result.get("spinning"):
                # LAD-fea-14: A 3D REFINEMENT THAT SPINS is the part's, not ours - gmsh kept inserting
                # points and rejecting nearly all of them - so the pass is a fill that failed: no mesh
                # came back, and the ladder goes on as after any failed fill (LAD-fea-2: HXT next)
                from meshpipeline.engines.gmsh.gmsh_runner import spin_words
                refine = (result.get("watchdog") or {}).get("refine") or {}
                _cite(job_id, [{"stage": "fea_ladder", "decided": "spinning", "value": refine.get("iterations"),
                                "rule": "LAD-fea-14", "inputs": {"pass": n, **refine},
                                "words": (spin_words((refine.get("iterations"), refine.get("kept"),
                                                      refine.get("rejected"))).capitalize()
                                          + f", so pass {n} was stopped as a fill that failed.")}])
            elif spent:
                # LAD-fea-15: read on as a fill that failed (no mesh came back): the ladder's levers, then the rung
                pass
            elif result.get("timed_out"):
                # A GUARD TRIP is our infrastructure, never the part's: it pulls no lever and changes no
                # plan. The attempt ends here; the run's own retry takes it again (D3: replayed once)
                run.note_native_run(produced_usable_mesh=False)
                tripped = {**work, "pass": n, "stalled": bool(result.get("stalled")),
                           "watchdog": result.get("watchdog") or {}}
                _cite(job_id, [{"stage": "fea_time", "decided": "stalled" if tripped["stalled"] else "ceiling",
                                "value": (tripped["watchdog"] or {}).get("seconds"), "rule": "PLAN-fea-13",
                                "inputs": {"pass": n, **(tripped["watchdog"] or {})},
                                "words": watchdog_words(tripped).capitalize() + "."}])
                break
            if result.get("too_big_for_worker"):
                # BUILT, BUT TOO BIG FOR THIS WORKER'S DISK (adapters/mesh_execution/cloud_run_client.
                # worker_room): ours, never the part's - no lever, no smaller plan; the result is kept
                run.note_native_run(produced_usable_mesh=False)
                too_big = {**result["too_big_for_worker"], "pass": n}
                break
            q = _read_quality(ws) if result.get("rc") in (0, 4) else {}
            made = bool(q) and BF.present(ws, "mesh.inp")
            run.note_native_run(produced_usable_mesh=made and result.get("rc") == 0)
            # LAD-fea-7: killed by the job's memory limit (the kernel's SIGKILL on the mesher)
            killed = _killed(result)
            passes.append({**L.pass_facts(n, result.get("rc"), q, plan, log=str(result.get("log_tail") or ""),
                                          killed=killed), "native_s": native_s,
                           # RUN-4: where it ran, and why
                           "ran_on": {k: machine.get(k) for k in ("where", "machine", "memory_gib", "why")}})
            if not passes[-1].get("valid"):
                # LAD-fea-8 riding along: the faces gmsh switched to MeshAdapt by itself in this failed pass
                try:
                    switched = L.switched_surfaces((ws / "gmshfea.log").read_text(errors="replace"))
                except OSError:
                    switched = []
                if switched:
                    passes[-1]["switched"] = switched
            _keep_pass_log(ws, n)
            if made:
                _keep_pass(ws, n)
            else:
                # WHERE IT FAILED, as the adapter wrote it beside the mesh (never lost under a traceback)
                said = _read_failure(ws)
                if said:
                    passes[-1]["bad_faces"] = passes[-1].get("bad_faces") or said.get("bad_faces") or []
                    passes[-1]["bad_surfaces"] = passes[-1].get("bad_surfaces") or said.get("bad_surfaces") or []
            if ((record.get("assembly_plan") or {}).get("mode") == "bonded" and not passes[-1].get("valid")
                    and (_fused_fill_failed(result, passes[-1]) if not made else _fused_joints_invalid(q))):
                # PLAN-fea-17 AT ONCE: the fill could not build the FUSED bonded parts (a volume left
                # empty, or the fused boundary crossing itself where parts pass into each other) - no
                # other 3D algorithm fixes the geometry, so the parts go straight to being meshed apart
                # and tied (fc_computer_desk: "BRep contains more volumes than there really are";
                # fc_folding_chair, 24 Oct: "PLC Error: A segment and a facet intersect", then 67 min
                # of passes that could only fail the same way)
                _cite(job_id, [{"stage": "fea_ladder", "decided": "tie now", "value": "fused fill failed",
                                "rule": "PLAN-fea-17", "inputs": {"pass": n},
                                "words": "The fused parts could not be filled as one, so the parts are tied now."}])
                break
            k_built = reaim_factor(plan, passes[-1]) if correction is None else None
            if k_built is not None:
                # PLAN-fea-31: the mesh is valid but its count is off what the user asked by more than REAIM_SHARE -
                # the plan is solved again with the measured built-over-planned, and built again (once)
                passes[-1]["off_count"] = True
                correction, reaim = k_built, True
                _cite(job_id, [{"stage": "fea_plan", "decided": "re-aimed on the built count", "value": round(k_built, 4),
                                "rule": "PLAN-fea-31",
                                "inputs": {"asked": (plan.get("target") or {}).get("asked"), "pass": n,
                                           "planned": plan["estimate"]["elements"], "built": passes[-1]["cells"]},
                                "words": (f"Pass {n} built {passes[-1]['cells']:,} elements where about "
                                          f"{plan['estimate']['elements']:,} were planned, off the count you asked for; "
                                          "the plan is solved again with that measured and built again.")}])
                break
            found = crossing_located(record, state) if not passes[-1].get("built") else None
            if found and not passes[-1].get("out_of_memory") and L.lever_options(passes[-1], levers, plan):
                # LAD-fea-16: A FILL THAT FAILED ON A PART WHOSE CAD FACES CROSS - located before meshing - is not
                # sent round gmsh's algorithm swaps (job bc3d4271, 1 Nov: 29 minutes, then every face on MeshAdapt);
                # the ladder ends here and the last rung meshes it (LAD-fea-9; a bonded assembly is tied first)
                crossing_found = found
                _cite(job_id, [{"stage": "fea_ladder", "decided": "crossing faces", "value": "last rung",
                                "rule": "LAD-fea-16", "inputs": {"pass": n, **found},
                                "words": crossing_words(found, n)}])
                break
            # AI-fea-8: the AI may pick among the levers this failure allows; the ladder's own first else
            lever, picked = await FA.choose_lever(L.lever_options(passes[-1], levers, plan), passes[-1], ai=ai,
                                                  state=state or {}, job_id=job_id, run=run,
                                                  wait_s=ai_wait_s(run))
            if picked:
                from meshpipeline.contracts.decision_record import SOURCE_AI_LEVER

                _cite(job_id, [{"stage": "fea_ai", "decided": "lever choice", "value": picked.get("picked"),
                                "rule": "AI-fea-8", "inputs": picked,
                                "source": SOURCE_AI_LEVER if picked.get("status") == "used" else "rule",
                                "words": (f"The AI picked {picked.get('picked')}: {picked.get('reason')}"
                                          if picked.get("status") == "used" else
                                          f"The AI's lever was not used ({picked.get('note')}); the ladder's own.")}])
            if lever is None:
                if passes[-1].get("out_of_memory"):
                    _cite(job_id, [{"stage": "fea_ladder", "decided": "no lever", "value": "out of memory",
                                    "rule": "LAD-fea-7", "inputs": {"pass": n, "estimate": passes[-1].get("estimate"),
                                                                    "machine": machine.get("machine")},
                                    "words": ("The mesher ran out of memory on the largest machine allowed; no lever "
                                              "builds this plan in less, so no retry.")}])
                break
            _cite(job_id, [{"stage": "fea_ladder", "decided": "lever", "value": lever["lever"], "rule": lever["lever"],
                            "inputs": lever.get("inputs") or {}, "words": lever["words"]}])
            levers.append(lever)

        if (tripped is None and too_big is None and unrun is None and passes and not any(f["valid"] for f in passes)
                and not passes[-1].get("out_of_memory")
                and (record.get("assembly_plan") or {}).get("mode") == "bonded"):
            # PLAN-fea-17: A BONDED ASSEMBLY THE MESHER COULD NOT BUILD AS ONE (the fused parts left
            # slivers it cannot fill): the parts are meshed apart and tied where they meet - still
            # bonded, never a stop. Not after a memory kill: that ends the attempt (LAD-fea-7)
            record = await asyncio.to_thread(restage_tied, ws, record)
            _cite(job_id, [{"stage": "fea_ladder", "decided": "tied", "value": "tie", "rule": "PLAN-fea-17",
                            "inputs": {"passes": len(passes)},
                            "words": ("No pass built the bonded parts as one mesh, so each part is meshed on its "
                                      "own and tied where they meet.")}])
            n0, levers = len(passes), []
            continue
        if reaim:
            reaim = False
            n0, levers = len(passes), []
            continue
        oom_bigger = bool(passes and passes[-1].get("out_of_memory")
                          and (sizes or int((first_plan.get("target") or {}).get("rules_elements") or 0)
                               >= int((first_plan.get("estimate") or {}).get("elements") or 0)))
        if (tripped is None and too_big is None and unrun is None and passes and first_plan
                and not any(f["valid"] for f in passes) and not by_choice_failed and not oom_bigger
                and crossing_found is None and (sizes or L.by_choice(first_plan))):
            # PLAN-fea-29/30: THE USER'S CHOICES NEVER FAIL THE RUN. No pass built a valid mesh of the plan their
            # count or sizes made: the rules' own plan is meshed instead, and said. Not when the fill failed on CAD faces
            # found crossing (LAD-fea-16): that is the part's geometry, not the user's choice, and goes to the last rung
            by_choice_failed = ("sizes" if sizes else "count")
            _cite(job_id, [{"stage": "fea_ladder", "decided": "the rules' own plan", "value": by_choice_failed,
                            "rule": "PLAN-fea-30", "inputs": {"passes": len(passes)},
                            "words": ("No pass built a valid mesh of the plan your " + ("sizes" if sizes else "element count")
                                      + " made, so the rules' own plan is meshed instead.")}])
            target, sizes = None, {}
            n0, levers = len(passes), []
            continue
        break

    if (tripped is None and too_big is None and unrun is None and passes and first_plan
            and not any(f["valid"] for f in passes) and not passes[-1].get("out_of_memory")):
        # LAD-fea-9, THE LAST RUNG: every gmsh option is spent (Delaunay, HXT, MeshAdapt on the surfaces
        # that failed, and for an assembly the tie) and no pass is valid - fTetWild meshes the part(s)
        # still without one, the other parts as the tie meshed them. Never for our infrastructure (a
        # guard trip, a worker too small) or a plan out of memory on the largest machine (LAD-fea-7)
        record, ran = await _last_rung(ws, record, diag_m, first_plan, passes, job_id=job_id, run=run,
                                       publish=publish, crossing=crossing_found)
        passes.extend(ran)
        if ran and ran[-1].get("not_run") and not any(f["valid"] for f in ran):
            last = ran[-1]
            unrun = {"pass": last["pass"], "error": last.get("rung_error"),
                     "machine": (last.get("ran_on") or {}).get("machine"), "where": (last.get("ran_on") or {}).get("where")}
            _cite(job_id, [{"stage": "fea_machine", "decided": "not run", "value": unrun["where"], "rule": "RUN-4",
                            "inputs": unrun, "words": not_run_words(unrun).capitalize() + "."}])
    if unrun is not None and not any(f["valid"] for f in passes):
        (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": None, "levers": levers,
                                                        "not_run": unrun}, indent=1))
        logger.error("fea driver: %s - job_id=%s", not_run_words(unrun), job_id)
        # told as OURS (run_infrastructure: the run may be taken again), never as the part's geometry
        _refuse(ws, cause=FailureCause.RUN_INFRASTRUCTURE.value, gate=NOT_RUN_GATE, text=not_run_words(unrun),
                deterministic=False, ours=True, not_run=unrun)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, exhausted=False, failure_marker=NOT_RUN)
        return False, NOT_RUN, outcome
    if too_big is not None and not any(f["valid"] for f in passes):
        (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": None, "levers": levers,
                                                        "too_big_for_worker": too_big}, indent=1))
        _refuse(ws, cause=FailureCause.RUN_INFRASTRUCTURE.value, gate=GUARD_GATE,
                text=(f"the mesh was built, but collecting it needs about {too_big['need_bytes'] / 1e9:.1f} GB "
                      f"and this worker has {too_big['free_bytes'] / 1e9:.1f} GB free"),
                deterministic=False, ours=True, result_uncollected=True, too_big_for_worker=too_big)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, exhausted=False, failure_marker=TOO_BIG)
        return False, TOO_BIG, outcome
    if tripped is not None and not any(f["valid"] for f in passes):
        (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": None, "levers": levers,
                                                        "guard_trip": tripped}, indent=1))
        logger.warning("fea driver: %s - job_id=%s", watchdog_words(tripped), job_id)
        # told as OURS (run_infrastructure: the run may be taken again), never as the part's detail
        # level. REBASE NOTE (#259): becomes determinism.note_guard_trip, which the graph replays once
        _refuse(ws, cause=FailureCause.RUN_INFRASTRUCTURE.value, gate=GUARD_GATE, text=watchdog_words(tripped),
                deterministic=False, ours=True, guard_trip=tripped)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, exhausted=False, failure_marker=GUARD_TRIP)
        return False, GUARD_TRIP, outcome
    closer_of_reaimed(passes, (first_plan.get("target") or {}).get("asked"))
    best, rule = L.best_pass(passes)
    _cite(job_id, [{"stage": "fea_ladder", "decided": "delivered pass", "value": best, "rule": "LAD-fea-5",
                    "inputs": {"passes": passes, "deciding": rule},
                    "words": (f"Pass {best} of {len(passes)} delivered." if best else "No pass built a valid mesh.")}])
    ok = best is not None
    if best is not None:
        _restore_pass(ws, best)
        _cite(job_id, open_pairs_decision(ws))
        built_remark(ws, record)
    (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": best, "levers": levers}, indent=1))
    if not ok and passes:
        # THE LADDER RAN OUT on a mesh it built: every pass this plan allows was tried, so the same
        # part at the same level builds the same mesh again. Said as the FEA's own outcome; the
        # executor still judges the mesh on disk, and this record tells the retry policy (and the
        # user's sentence) that another attempt on this engine cannot change it (job ab20566f
        # spent a second identical 14-minute attempt)
        defect = defect_at_failure(record, passes)
        if defect is not None:
            # MESH-2: THE MESH FAILED WHERE A CHECK FLAGGED THE CAD - the one case the user is told the
            # part needs CAD repair, with the faces named (detect only: nothing was healed)
            _cite(job_id, [{"stage": "fea_ladder", "decided": "cad defect", "value": defect["faces"],
                            "rule": "MESH-2", "inputs": defect,
                            "words": "The mesh failed at the faces a check had flagged: the CAD needs repair there."}])
            _refuse(ws, cause=FailureCause.MESH_QUALITY.value, gate=CAD_DEFECT_GATE,
                    text=f"the mesh failed at {', '.join(defect['faces'][:6])}, where the geometry check flagged a "
                         "possible CAD defect", cad_defect=defect, **ladder_facts(ws, passes, levers))
        else:
            _refuse(ws, cause=FailureCause.MESH_QUALITY.value, gate=QUALITY_GATE,
                    text=ladder_words(passes, levers), **ladder_facts(ws, passes, levers))
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok, failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


def applies(state) -> bool:
    """Whether this run is the FEA driver's: a structural purpose, a 3D solid, and no face the user
    named with a role (version 1 names every face Face_k for the user's own solver; a named
    support or load still goes through the builder loop, which maps the words onto faces). A 2D
    plane-stress case keeps the builder loop."""
    s = state or {}
    if str(s.get("purpose") or "").strip() != "structural":
        return False
    if str(s.get("dimensionality") or "").strip().upper() == "2D" and mesh_as_of(s) != "shell":
        # (a shell asked for in words like "2D elements" is still this driver's: SHSTAGE-1)
        return False
    return not any(isinstance(p, dict) and str(p.get("name") or "").strip() for p in s.get("intake_patches") or [])


def driver_for(state):
    """engines/gmsh/spec's run driver (EngineSpec.build_driver_for): this driver, or None."""
    return drive if applies(state) else None


__all__ = ["MIXED_GATE", "SHELL_RUN_LIMIT_S", "mesh_as_of", "shell_pass", "connection_of", "element_target", "target_plan", "prepare_assembly", "restage_tied", "BYTES_PER_ELEMENT", "MESH_JOB_MEMORY_GIB", "limit_facts", "memory_elements", "FILL_RATES", "FLOOR_STEPS", "WORKSPACE_BYTES_PER_ELEMENT", "memory_need", "MESH_JOB_LIMIT_S", "VM_CEILING_S", "attempt_left_s", "ceiling_for", "planned_seconds", "choose_plan",
           "complexity", "element_limit", "fill_rate", "watchdog_words", "BUILD_FAILED", "GUARD_TRIP", "NO_SOURCE", "QUALITY_GATE", "applies", "drive",
           "driver_for", "NOT_RUN", "NOT_RUN_GATE", "not_run", "not_run_words", "ladder_facts", "ladder_words", "make_plan", "measure_staged", "planned_work", "write_plan"]
