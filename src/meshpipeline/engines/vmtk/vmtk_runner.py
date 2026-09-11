# Responsibility: Drive a VMTK run: remesh the surface, close the lumen, compute centrelines and fill the volume.
# Owns: the vascular stage sequence and the artifacts each stage leaves for review.
# Boundaries: cell size adapts to local radius, so a narrow vessel is resolved without refining the whole domain.
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
from meshpipeline.engines.base import RunPolicy
from meshpipeline.sandbox.safe_exec import (
    NativeOutcome,
    describe_native_result,
    run_guarded,
)

logger = logging.getLogger(__name__)

_LUMEN = "lumen.vtp"
_CENTERLINES = "centerlines.vtp"
_DIST = "lumen_dist.vtp"
_MESH = "mesh.vtu"
_LUMEN_OPEN = "lumen_open.vtp"     # the staged open wall (engines/vmtk/lumen_staging.py)
#: A boundary layer whose tets sum to more than this above the enclosed volume has folded;
#: tets summing to less than the enclosed volume by more than this never filled it.
OVERLAP_TOLERANCE = 0.02

_DEFAULTS: dict = {
    "edge_length_factor": 0.3,
    "boundary_layers": 3,
    "boundary_layer_thickness_factor": 0.2,
    "cap_openings": True,
    "remesh_surface": True,
    "max_cells": 4_000_000,
    # ENGINE-STAGED sizing. lumen_staging fills these from the declared ports of a CAD body
    # (configure_mesh merges them); empty = nothing staged, the pype runs on the lumen as given.
    "sizing_array": "",
    "min_edge_length": None,
    "max_edge_length": None,
    # staged route only: whether vmtkmeshgenerator remeshes the (already remeshed) surface
    # again before capping and filling; the repair ladder toggles it
    "generator_remesh": True,
    # CENTERLINE SEEDING - must be NON-INTERACTIVE. vmtk's 'openprofiles'/'pickpoint'
    # selectors open an X render window and abort in a headless worker (verified: SIGABRT,
    # "bad X server connection"). The non-interactive selectors are:
    #   profileidlist  -> -sourceids/-targetids   (ids of the lumen's OPEN profiles)
    #   pointlist      -> -sourcepoints/-targetpoints (explicit coords; a CLOSED lumen)
    "source_ids": [],
    "target_ids": [],
    "source_points": [],
    "target_points": [],
}


def resolve_strategy(strategy: dict | None) -> dict:
    s = dict(_DEFAULTS)
    s.update({k: v for k, v in (strategy or {}).items() if k in _DEFAULTS})
    return s


# the pype (pure, unit-tested)

def _seed_coord(value) -> str:
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"seed coordinate must be a finite number, got {value!r}")
    return repr(v)


def _seed_args(s: dict) -> list[str]:
    if s["source_points"] and s["target_points"]:
        return (["-seedselector", "pointlist",
                 "-sourcepoints", *[_seed_coord(v) for v in s["source_points"]],
                 "-targetpoints", *[_seed_coord(v) for v in s["target_points"]]])
    if s["source_ids"] and s["target_ids"]:
        return (["-seedselector", "profileidlist",
                 "-sourceids", *[str(int(v)) for v in s["source_ids"]],
                 "-targetids", *[str(int(v)) for v in s["target_ids"]]])
    raise ValueError(
        "vmtk centerline seeding requires either source_points+target_points or "
        "source_ids+target_ids, both taken from the lumen's OPEN inlet/outlet profiles "
        "(geometry_report lists them). Interactive seeding is impossible in a headless worker.")


def build_staged_stages(strategy: dict) -> tuple[list[str], list[str]]:
    """The STAGED CAD LUMEN route as two argv lists: the surface stage, run once, and the
    generator stage, run per repair-ladder step. The open wall arrives with its local radius
    at every point (engines/vmtk/lumen_staging.py).
    Surface: 1. remesh it radius-adaptively (the staged triangles are edge-bounded CAD
    slivers; handing those straight to the generator left TetGen an inner surface it refused -
    "Unable to find an edge in subface" on tee_wye_003 and straight_reducer_004); 2. keep the
    one lumen and drop the orphan points the remesher leaves; 3. project the radius array back
    from the staged wall (the remesher drops point data).
    Generate: vmtkmeshgenerator caps the declared openings (one CellEntityId each, from 2),
    optionally remeshes with the array again, grows the layers and fills the volume. Capping is
    not optional here - the surface is open by construction - so cap_openings does not apply;
    generator_remesh stands in for remesh_surface. No centerline stage: vmtkcenterlines was the
    part that failed (see local_radius)."""
    s = resolve_strategy(strategy)
    elf = float(s["edge_length_factor"])
    array = str(s["sizing_array"])
    surface = [
        rtcfg.VMTK_BIN,
        "vmtksurfaceremeshing", "-ifile", _LUMEN_OPEN,
        "-elementsizemode", "edgelengtharray", "-edgelengtharray", array,
        "-edgelengthfactor", f"{elf:g}", "-preserveboundary", "0", "-iterations", "10",
        "--pipe", "vmtksurfaceconnectivity", "-method", "largest",
        "--pipe", "vmtksurfaceprojection", "-rfile", _LUMEN_OPEN, "-ofile", _LUMEN,
    ]
    generate = [
        rtcfg.VMTK_BIN, "vmtkmeshgenerator", "-ifile", _LUMEN,
        "-elementsizemode", "edgelengtharray", "-edgelengtharray", array,
        "-edgelengthfactor", f"{elf:g}", *_clamp_args(s),
        "-skipcapping", "0", "-skipremeshing", "0" if s.get("generator_remesh", True) else "1",
        *_layer_args(s), "-tetrahedralize", "1", "-ofile", _MESH,
    ]
    return surface, generate


def _clamp_args(s: dict) -> list[str]:
    # clamps on the radius-adaptive size: where the sizing field touches zero (a ray that
    # grazes a corner, centerlines merging) a zero-size target kills the generator
    return [
        *(["-minedgelength", f"{float(s['min_edge_length']):g}"]
          if s.get("min_edge_length") else []),
        *(["-maxedgelength", f"{float(s['max_edge_length']):g}"]
          if s.get("max_edge_length") else []),
    ]


def _layer_args(s: dict) -> list[str]:
    layers = int(s["boundary_layers"])
    out = ["-boundarylayer", "1" if layers > 0 else "0"]
    if layers > 0:
        out += [
            # vmtk's layer COUNT flag is -sublayers (there is no -numberoflayers)
            "-sublayers", str(layers),
            "-thicknessfactor", f"{float(s['boundary_layer_thickness_factor']):g}",
            # layers belong on the lumen WALL, not across the inlet/outlet caps
            "-boundarylayeroncaps", "0",
        ]
    return out


def build_pype(strategy: dict) -> list[str]:
    s = resolve_strategy(strategy)
    elf = float(s["edge_length_factor"])
    clamps = _clamp_args(s)
    layer_args = _layer_args(s)
    if s.get("sizing_array"):
        surface, generate = build_staged_stages(s)
        return surface + ["--pipe"] + generate[1:]
    argv: list[str] = [
        rtcfg.VMTK_BIN,
        # 1. centerlines of the lumen, seeded NON-INTERACTIVELY
        "vmtkcenterlines", "-ifile", _LUMEN, *_seed_args(s),
        "-ofile", _CENTERLINES,
        # 2. tag every surface point with its LOCAL RADIUS (distance to centerline)
        "--pipe", "vmtkdistancetocenterlines", "-ifile", _LUMEN,
        "-centerlinesfile", _CENTERLINES, "-useradius", "1", "-ofile", _DIST,
        # 3. radius-adaptive surface remesh + tetrahedral volume fill (+ boundary layers)
        "--pipe", "vmtkmeshgenerator", "-ifile", _DIST,
        "-elementsizemode", "edgelengtharray",
        "-edgelengtharray", "DistanceToCenterlines",
        "-edgelengthfactor", f"{elf:g}", *clamps,
        # vmtkmeshgenerator expresses these as the INVERSE (skip-*) booleans
        "-skipcapping", "0" if s["cap_openings"] else "1",
        "-skipremeshing", "0" if s["remesh_surface"] else "1",
        *layer_args,
    ]
    argv += ["-tetrahedralize", "1", "-ofile", _MESH]
    return argv


# geometry staging + inspection

def _read_surface(path: Path):
    import pyvista as pv
    return pv.read(str(path))


def tessellate_to_stl(geom_path, out_stl, *, context=None, prepared=None) -> Path:
    import pyvista as pv
    geom_path, out_stl = Path(geom_path), Path(out_stl)
    ws = out_stl.parent
    suffix = geom_path.suffix.lower()
    if suffix in (".step", ".stp", ".iges", ".igs"):
        from meshpipeline.cad.cad_tessellate import tessellate_to_stl as _cad
        _cad(geom_path, out_stl, prepared=prepared)    # CAD -> surface STL, already metres
        surf = _read_surface(out_stl)
    elif suffix == ".stl":
        if geom_path.resolve() != out_stl.resolve():
            shutil.copy2(geom_path, out_stl)
        surf = _read_surface(out_stl)
    else:                                              # .vtp / .vtk - vmtk's native surface
        surf = _scaled_polydata(geom_path, prepared)
        pv.PolyData(surf).extract_surface().save(str(out_stl))   # preview, from the SAME points
    pv.PolyData(surf).extract_surface().save(str(ws / _LUMEN))
    return out_stl


def _scaled_polydata(vtp_path: Path, prepared):
    factor = _metre_factor(prepared)
    mesh = _read_surface(vtp_path)
    if factor != 1.0:
        # points only: `mesh.points = ...` replaces the coordinate array and leaves cells,
        # point data and cell data attached exactly as they were.
        mesh.points = mesh.points * factor
    return mesh


def _metre_factor(prepared) -> float:
    if prepared is None:
        raise ValueError(
            "the vmtk bundle needs the typed coordinate state to place its lumen in metres; "
            "without it the physical size of the vessel would be a guess")
    return prepared.to_metres


def inspect_stl(workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
    ws = Path(workspace)
    lumen = ws / _LUMEN
    if not lumen.exists():
        return {"error": f"{_LUMEN} missing - the workspace was not staged for vmtk"}
    surf = _read_surface(lumen).extract_surface()
    edges = surf.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                       manifold_edges=False, non_manifold_edges=False)
    profiles = []
    if edges.n_cells:
        bodies = edges.connectivity().split_bodies()
        for i, b in enumerate(bodies):
            c = b.center
            profiles.append({"index": i, "centroid": [round(float(v), 6) for v in c],
                             "n_edge_cells": int(b.n_cells)})
    b = surf.bounds
    diag = ((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) ** 0.5
    from meshpipeline.engines.vmtk.lumen_staging import read_staging
    staged = read_staging(ws)
    if staged and staged.get("ports"):
        return {
            "closed": len(profiles) == 0,
            "open_profiles": profiles,
            "n_open_profiles": len(profiles),
            "bbox": [b[0], b[2], b[4], b[1], b[3], b[5]],
            "diag": diag,
            "n_surface_cells": int(surf.n_cells),
            # the engine opened the declared ports itself: name, role, where, how big
            "staged_ports": [{"name": p["name"], "role": p["role"],
                              "centroid": [round(float(v), 6) for v in p["centroid"]],
                              "equivalent_diameter_m": round(float(p["size_m"]), 6)}
                             for p in staged["ports"]],
            "seeds_staged": bool(staged.get("source_points") and staged.get("target_points")),
            "sizing_staged": bool(staged.get("sizing_array")),
            "local_radius_m": staged.get("radius_m"),
            "note": ("The engine has ALREADY opened this CAD body at the declared inlet/outlet "
                     "faces (lumen.vtp is the fluid wall with real holes) and measured the "
                     "lumen's local radius at every wall point, which is what the cells are "
                     "sized from (edge_length_factor x local radius, clamped by "
                     "min_edge_length / max_edge_length). No centerline seeds are needed: "
                     "configure_mesh takes the staged sizing - leave seeds and the clamps out "
                     "unless a run_mesh failure names one to change. Each staged port becomes "
                     "one capped patch under its declared name."),
        }
    return {
        "closed": len(profiles) == 0,
        "open_profiles": profiles,
        "n_open_profiles": len(profiles),
        "bbox": [b[0], b[2], b[4], b[1], b[3], b[5]],
        "diag": diag,
        "n_surface_cells": int(surf.n_cells),
        "note": ("Each open profile is a candidate inlet/outlet cap - map them onto the "
                 "contracted patch names. If any exist, keep cap_openings=true. A surface with "
                 "NO open profiles cannot be meshed by this engine: vmtk takes its centerline "
                 "endpoints from real boundary loops, and a sealed anatomy states none."),
    }


# configure (write the spec)

def configure_mesh(workspace, *, strategy: dict, wall_patch: str = "",
                   geometry_file: str = "input.stl", **_ignored) -> dict:
    ws = Path(workspace)
    # WHERE THE ENGINE'S INPUT CONTRACT IS ENFORCED. vmtk derives its centerline endpoints from
    # the surface's real boundary loops - the inlet and outlet the anatomy actually has. A sealed
    # lumen states none, and the `pointlist` selector cannot invent them: it maps a coordinate to
    # the nearest WALL VERTEX (vtkPointLocator.FindClosestPoint), and vmtkcenterlines caps only
    # for the open-profile selectors, so a closed surface yields a collapsed two-point path and
    # then a volume fill that produces nothing.
    #
    # Refusing here means no centerline and no mesh generator ever start, so the user gets a
    # statement about their geometry instead of a native run that fails several minutes later for
    # reasons only a log explains. Deciding WHERE to cut a closed anatomy open is a modelling
    # decision this engine does not make.
    topology = inspect_stl(ws, geometry_file=geometry_file)
    if not topology.get("error") and int(topology.get("n_open_profiles", 0)) == 0:
        return {"code": "vmtk_requires_open_profiles",
                "n_open_profiles": 0,
                "error": ("this surface is closed - it has no open inlet/outlet profiles, and "
                          "vmtk takes its centerline endpoints from those openings. Supply the "
                          "lumen with its terminal openings present (the inlet and outlet cut "
                          "open, not sealed), or mesh the sealed solid with a volume engine "
                          "instead.")}
    from meshpipeline.engines.vmtk.lumen_staging import merge_staged, read_staging
    s = resolve_strategy(merge_staged(strategy, read_staging(ws)))
    if not s.get("sizing_array") and not ((s["source_points"] and s["target_points"])
                                          or (s["source_ids"] and s["target_ids"])):
        return {"code": "vmtk_seeds_required",
                "error": ("centerline seeding is required and nothing was staged for this "
                          "geometry: give source_points+target_points (coordinates on the "
                          "inlet and outlet ends) or source_ids+target_ids (open-profile ids "
                          "from geometry_report). Interactive seeding cannot run headless.")}
    (ws / "vmtk_spec.json").write_text(json.dumps(s, indent=2))
    return {"spec": s, "pype": " ".join(build_pype(s))}


# staging (builder attempt seam): open the declared ports of a CAD body before anything runs

def stage_declared(workspace, *, geometry_path, prepared, intake_patches: list,
                   input_kind: str = "") -> dict | None:
    """Deterministic lumen preparation - see engines/vmtk/lumen_staging.py. Returns the
    staging record, or None when it does not apply (a surface input, nothing declared)."""
    from meshpipeline.engines.vmtk.lumen_staging import stage_lumen
    return stage_lumen(workspace, geometry_path, prepared=prepared,
                       intake_patches=intake_patches, input_kind=input_kind)


# run (isolated subprocess)

def _native_payload_members(workspace) -> list[str]:
    """What the remote vmtk pype consumes: the spec (argv is built from it there) and the staged
    lumen. Everything else in the attempt's workspace is local-only or a PRIOR pass's output
    (mesh.vtu, centerlines.vtp, the distance surface, log.vmtk) that must not ride along."""
    ws = Path(workspace)
    return [n for n in ("vmtk_spec.json", _LUMEN, "lumen_open.vtp") if (ws / n).exists()]


def run_cartesian_mesh(workspace, *, timeout: int, context=None) -> dict:
    from meshpipeline.contracts.mesh_execution import (
        note_native_pass,
        note_native_payload,
        read_native_pass,
        run_mesh,
    )
    # THE PASS IS PART OF THE SUBMISSION IDENTITY (job + generation + attempt + pass). The
    # builder may run the mesher several times in one attempt with a revised spec - remesh on,
    # coarser edge length, layers off - and without a recorded pass every run after the first
    # was refused as a conflicting replay of the first one's claim ("claimed for engine 'vmtk'
    # with a different payload"; jobs 73cce02e and 65081ced, 8 Sep). Numbered from the workspace
    # fact so the count survives the tool being called from a fresh loop.
    ws = Path(workspace)
    # Facts are workspace files: recorded when there is a workspace to record them in. A run
    # tool handed a path that does not exist (the dispatch-contract test does) still dispatches
    # through the contract, which then reports the missing case itself.
    if ws.is_dir():
        note_native_pass(ws, (read_native_pass(ws) or 0) + 1)
        note_native_payload(ws, _native_payload_members(ws))
    return run_mesh(ws, engine="vmtk", timeout=timeout)


def repair_ladder(strategy: dict) -> list[dict]:
    """The generator strategies a staged run tries in order when TetGen does not complete:
    as given; without the generator's second remesh; that with half the layer thickness; no
    layers; no layers and no second remesh. TetGen's verdict on this class of surface flips on
    near-identical input - manifold_002 filled in the lab, refused the same wall (exception,
    then a segfault without layers) in the image, and filled again in 8 s once the generator
    skipped its own remesh - so the ladder varies the surface it sees, not just the layers.
    The staged wall is clean; the engine walks the ladder itself instead of spending builder
    turns, and a layer-free radius-adaptive fill is a valid deliverable. Unstaged runs (a
    user's own .vtp) keep the single attempt."""
    s = resolve_strategy(strategy)
    if not s.get("sizing_array"):
        return [s]
    steps = [s]
    if s.get("generator_remesh", True):
        steps.append({**s, "generator_remesh": False})
    if int(s.get("boundary_layers") or 0) > 0:
        steps.append({**s, "generator_remesh": False,
                      "boundary_layer_thickness_factor":
                          float(s["boundary_layer_thickness_factor"]) / 2.0})
        steps.append({**s, "boundary_layers": 0})
        steps.append({**s, "boundary_layers": 0, "generator_remesh": False})
    return steps


def _step_label(strat: dict) -> str:
    return (f"layers={strat['boundary_layers']}, thickness factor "
            f"{float(strat['boundary_layer_thickness_factor']):g}, generator remesh "
            f"{'on' if strat.get('generator_remesh', True) else 'off'}")


def _fill_completed(ws: Path, result: dict) -> bool:
    if result.get("rc") not in (0, None) or result.get("timed_out"):
        return False
    if not (ws / _MESH).exists():
        return False
    low = (result.get("log_tail") or "").lower()
    return not any(f in low for f in _TETGEN_FAILURES)


def _run_vmtk_local(workspace, *, timeout: int, **_ignored) -> dict:
    ws = Path(workspace)
    spec_path = ws / "vmtk_spec.json"
    if not spec_path.exists():
        return {"rc": 1, "timed_out": False,
                "log_tail": "vmtk_spec.json missing - call configure_mesh before run_mesh"}
    strategy = json.loads(spec_path.read_text())
    ladder = repair_ladder(strategy)
    notes: list[str] = []
    result: dict = {}
    staged = bool(resolve_strategy(strategy).get("sizing_array"))
    if staged:
        # the surface stage once; the generator per ladder step
        surface, _ = build_staged_stages(ladder[0])
        result = _run_pype(ws, surface, timeout=timeout)
        if result.get("rc") not in (0, None) or result.get("timed_out") \
                or not (ws / _LUMEN).exists():
            (ws / "log.vmtk").write_text(result.get("log_tail") or "")
            return result
    i = 0
    for i, strat in enumerate(ladder):
        argv = build_staged_stages(strat)[1] if staged else build_pype(strat)
        if i:
            (ws / _MESH).unlink(missing_ok=True)
        result = _run_pype(ws, argv, timeout=timeout)
        if _fill_completed(ws, result) or i == len(ladder) - 1:
            break
        notes.append(f"[vmtk] attempt {i + 1} ({_step_label(strat)}): TetGen did not complete "
                     f"the fill - next: {_step_label(ladder[i + 1])}")
    if notes:
        effective = dict(ladder[min(i, len(ladder) - 1)])
        effective["repair_note"] = "; ".join(notes)
        # the shipped spec says what was actually run (the deliverable re-runs from it)
        spec_path.write_text(json.dumps(effective, indent=2))
        result["log_tail"] = "\n".join(notes) + "\n" + (result.get("log_tail") or "")
        result["repair_note"] = effective["repair_note"]
    (ws / "log.vmtk").write_text(result.get("log_tail") or "")
    return result


def _run_pype(ws: Path, argv: list[str], *, timeout: int) -> dict:
    try:
        proc = run_guarded(argv, cwd=str(ws), capture_output=True, text=True, timeout=timeout)
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-2000:]
        # Signal interpretation is the SHARED seam's, not this bundle's. It lived here first,
        # which meant vmtk explained a SIGSEGV while the other four engines returned a bare
        # negative rc for the same event.
        return describe_native_result(returncode=proc.returncode, args=argv,
                                      stage="vmtk pype", output=tail)
    except FileNotFoundError:
        return {"rc": 127, "timed_out": False,
                "log_tail": (f"vmtk binary {rtcfg.VMTK_BIN!r} not found - the container's vmtk "
                             "conda env is not installed or not on PATH")}
    except subprocess.TimeoutExpired as exc:
        def _d(x):
            return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        tail = (_d(exc.stdout) + "\n" + _d(exc.stderr))[-2000:]
        return describe_native_result(returncode=-1, args=argv, stage="vmtk pype",
                                      output=tail, outcome=NativeOutcome.timed_out)


# quality read-back

_VTK_TETRA = 10   # cell type; mesh.vtu also carries the boundary TRIANGLES (type 5)
_WALL_ENTITY = 1  # vmtk's CellEntityId for the lumen wall; caps are numbered from 2

# TetGen reports a self-intersecting boundary like this and vmtk STILL exits 0, leaving a
# mesh whose tets are mostly inverted. The exit code alone can never be trusted.
_TETGEN_FAILURES = ("invalid plc", "subfaces intersect", "self-intersect",
                    # vmtkmeshgenerator catches the exception, exits 0 and writes the boundary
                    # layer alone ("Will only output surface mesh and boundary layer")
                    "tetgen quit with an exception", "error occurred during tetrahedralization")


def check_mesh(workspace) -> dict:
    from meshpipeline.engines.vmtk.criteria import QUALITY_FLOOR
    ws = Path(workspace)
    mp = ws / _MESH
    if not mp.exists():
        return {"cells": 0, "fatal": [f"{_MESH} missing - run_mesh has not produced a mesh yet"],
                "mesh_ok": False, "min_quality": None}
    # TRUNCATION GUARD: a crashed mesher (a live cloud run segfaulted, rc=-11) leaves a
    # partial .vtu; handing that to vtkXMLUnstructuredGridReader errored and then HUNG the
    # tool thread, stalling the whole build until the loop timeout. A well-formed VTU ends
    # with the closing root tag - check the tail bytes BEFORE invoking the reader at all.
    try:
        with open(mp, "rb") as _fh:
            _fh.seek(max(0, mp.stat().st_size - 512))
            _tail = _fh.read()
        if b"</VTKFile>" not in _tail:
            return {"cells": 0, "mesh_ok": False, "min_quality": None,
                    "fatal": [f"{_MESH} is truncated/corrupt (no closing tag - the mesher "
                              "likely crashed mid-write); re-run the mesh"]}
    except OSError as _exc:
        return {"cells": 0, "mesh_ok": False, "min_quality": None,
                "fatal": [f"{_MESH} unreadable: {_exc}"]}
    fatal: list[str] = []
    # rc==0 is NOT proof of success - catch the swallowed TetGen failure
    log = ws / "log.vmtk"
    if log.exists():
        low = log.read_text(errors="replace").lower()
        if any(f in low for f in _TETGEN_FAILURES):
            fatal.append("TetGen rejected the boundary as self-intersecting (Invalid PLC) - "
                         "the volume fill did not complete even though vmtk exited 0")
    mesh = _read_surface(mp)
    # NODE ORDER. vmtk writes its boundary-layer tets with the opposite node order from
    # TetGen's interior tets: every layer tet has a NEGATIVE signed volume while the layer
    # block itself is sound (the |volumes| sum to what the boundary encloses within 1%,
    # straight_reducer_004 and s_duct_001, 2026-09-11). Read as "inverted" that made every
    # layered mesh a fatal defect and taught the builder to drop layers. Normalise the order
    # once (idempotent) so the deliverable and the numbers below are right; a layer that
    # really folded is caught by the overlap test, which the sign cannot tell.
    mesh, n_reoriented, layer_mask = _normalise_orientation(mesh)
    if n_reoriented:
        mesh.save(str(mp))
    tets = mesh.extract_cells_by_type(_VTK_TETRA)
    cells = int(tets.n_cells)
    if cells == 0:
        # A SURFACE-ONLY artifact: vmtk exited 0 and wrote a well-formed .vtu, but TetGen produced
        # no volume. The verdict is returned HERE, before any filter that assumes a non-empty
        # tetrahedral set - VTK's cell-quality filter SEGFAULTS on an empty grid, and a segfault is
        # not a Python exception, so the best-effort `except` below cannot catch it. The validator
        # written to classify a failed mesh was taking the worker down with it instead.
        fatal.append(
            f"{_MESH} carries no tetrahedra ({int(mesh.n_cells)} surface cells only) - vmtk exited "
            "0 but the volume fill did not complete; rc 0 alone never proves a mesh")
        return {"cells": 0, "fatal": fatal, "min_quality": None,
                "layer_coverage": None, "mesh_ok": False}
    min_quality = None
    layer_min_quality = None
    try:
        import numpy as np
        vol = np.asarray(tets.compute_cell_sizes(length=False, area=False,
                                                 volume=True).cell_data["Volume"])
        n_inverted = int((vol <= 0.0).sum())
        if n_inverted:
            fatal.append(f"{n_inverted} inverted/degenerate tetrahedra (non-positive volume)")
        overlap = _overlap_fraction(mesh, float(np.abs(vol).sum()))
        if overlap is not None and overlap > OVERLAP_TOLERANCE:
            fatal.append(f"tetrahedra overlap: their volumes sum to {overlap * 100:.1f}% more "
                         "than the boundary encloses - the boundary layer folded into itself")
        elif overlap is not None and overlap < -OVERLAP_TOLERANCE:
            fatal.append(f"the volume fill is incomplete: the tetrahedra fill only "
                         f"{(1.0 + overlap) * 100:.0f}% of what the boundary encloses - TetGen "
                         "did not complete (a boundary layer alone is not a mesh)")
        # pyvista >=0.45: DataSet.cell_quality(measure) -> array named after the measure
        sj = np.asarray(tets.cell_quality("scaled_jacobian").cell_data["scaled_jacobian"])
        # THE FLOOR JUDGES THE ISOTROPIC FILL. A boundary-layer tet is a thin slab split three
        # ways - its scaled Jacobian is about thickness over edge length (median 0.08, a few
        # below 0.01 on manifold_002) BY DESIGN, not by defect; judged with the interior it
        # failed every layered mesh and taught the builder to drop layers. The layer block is
        # the block vmtk wrote negatively ordered (identified above); its own worst value is
        # reported alongside, and a collapsed layer still fails on volume or overlap.
        interior = sj[~layer_mask] if layer_mask is not None and layer_mask.size == sj.size else sj
        layer = sj[layer_mask] if layer_mask is not None and layer_mask.size == sj.size else sj[:0]
        min_quality = float(interior.min()) if interior.size else (
            float(layer.min()) if layer.size else None)
        layer_min_quality = float(layer.min()) if layer.size else None
    except Exception as exc:  # noqa: BLE001 - quality is best-effort evidence, not a crash
        logger.warning("vmtk check_mesh: quality computation failed: %s", exc)
    # LAYER COVERAGE is measured here, from the mesh: the share of wall triangles whose three
    # vertices belong to boundary-layer tets. The reviewer REQUIRES this metric for its
    # local_anatomical_fidelity axis (MetricRequirement("layer_coverage")) and nothing had
    # ever written layer_report.json, so the first delivery through the product
    # (straight_reducer_006, job 1d414282, 2026-09-11: 150,915 cells, every gate green) died
    # at the reviewer with "required deterministic evidence missing ('metric:layer_coverage',)".
    # A layer-free mesh reads 0 % - a number the reviewer can weigh, not a missing fact.
    layer_coverage, layer_facts = _layer_coverage(mesh, layer_mask)
    try:
        (ws / "layer_report.json").write_text(json.dumps(
            {"coverage": layer_coverage, **layer_facts,
             "method": "wall triangles whose vertices all belong to boundary-layer tets"},
            indent=2))
    except OSError as exc:
        logger.warning("vmtk check_mesh: could not write layer_report.json: %s", exc)
    ok = (not fatal) and cells > 0 and (min_quality is None or min_quality > QUALITY_FLOOR)
    return {"cells": cells, "fatal": fatal, "min_quality": min_quality,
            "layer_coverage": layer_coverage, "mesh_ok": ok,
            "reoriented_tets": n_reoriented,
            "layer_tets": int(layer_mask.sum()) if layer_mask is not None else 0,
            "layer_min_quality": layer_min_quality}


_LAYER_ARRAY = "BoundaryLayer"   # cell data on mesh.vtu: 1 = a boundary-layer tet, 0 = anything else


def _normalise_orientation(grid):
    """(grid, n, mask) with every negative-volume tetrahedron's node order flipped (nodes 1 and
    2 swapped); `mask` marks the boundary-layer tets in tet order, or is None when the mesh
    carries no layer. The flipped block is remembered in the BoundaryLayer cell array, so a
    later check (finalize runs one after the run tool's) judges the same tets the same way;
    with the sign gone, the second check used to fold the layer into the isotropic floor
    (bend_elbow_003: 0.0707 on the first check, 0.0064 on the second). Other cell types, cell
    data and point data ride along."""
    import numpy as np
    import pyvista as pv
    ct = np.asarray(grid.celltypes)
    tet_rows = ct == _VTK_TETRA
    if not tet_rows.any():
        return grid, 0, None
    cd = grid.cells_dict
    tets = np.asarray(cd[_VTK_TETRA], dtype=np.int64)
    p = np.asarray(grid.points, dtype=float)
    a, b, c, d = (p[tets[:, k]] for k in range(4))
    vol = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)
    neg = vol < 0.0
    n = int(neg.sum())
    if n == 0:
        flag = grid.cell_data.get(_LAYER_ARRAY)
        if flag is None:
            return grid, 0, None
        mask = np.asarray(flag)[tet_rows] > 0
        return grid, 0, (mask if mask.any() else None)
    tets = tets.copy()
    tets[neg, 1], tets[neg, 2] = tets[neg, 2].copy(), tets[neg, 1].copy()
    types = [int(t) for t in np.unique(ct)]
    cells = {t: (tets if t == _VTK_TETRA else np.asarray(cd[t])) for t in types}
    out = pv.UnstructuredGrid(cells, p)
    # the dict constructor lays cells out type by type; regroup the arrays the same way
    order = np.concatenate([np.flatnonzero(ct == t) for t in types])
    for name in list(grid.cell_data.keys()):
        out.cell_data[name] = np.asarray(grid.cell_data[name])[order]
    for name in list(grid.point_data.keys()):
        out.point_data[name] = np.asarray(grid.point_data[name])
    flag = np.zeros(len(ct), dtype=np.int8)
    flag[np.flatnonzero(tet_rows)[neg]] = 1
    out.cell_data[_LAYER_ARRAY] = flag[order]
    return out, n, neg


def _layer_coverage(grid, layer_mask) -> tuple[float, dict]:
    """(coverage %, facts): the share of wall triangles (CellEntityIds == 1, or every boundary
    triangle when the mesh carries no ids) whose three vertices all belong to a boundary-layer
    tet. 0.0 when the mesh has no layer block."""
    import numpy as np
    ct = np.asarray(grid.celltypes)
    tri_rows = ct == 5
    if not tri_rows.any():
        return 0.0, {"wall_triangles": 0, "covered_wall_triangles": 0, "layer_tets": 0}
    tris = np.asarray(grid.cells_dict[5], dtype=np.int64)
    ids = grid.cell_data.get("CellEntityIds")
    if ids is not None:
        wall = np.asarray(ids)[tri_rows] == _WALL_ENTITY
        tris = tris[wall] if wall.any() else tris
    n_wall = int(len(tris))
    if layer_mask is None or not np.asarray(layer_mask).any() or not (ct == _VTK_TETRA).any():
        return 0.0, {"wall_triangles": n_wall, "covered_wall_triangles": 0, "layer_tets": 0}
    tets = np.asarray(grid.cells_dict[_VTK_TETRA], dtype=np.int64)[np.asarray(layer_mask)]
    in_layer = np.zeros(grid.n_points, dtype=bool)
    in_layer[np.unique(tets)] = True
    covered = int(in_layer[tris].all(axis=1).sum())
    pct = 100.0 * covered / n_wall if n_wall else 0.0
    return round(pct, 2), {"wall_triangles": n_wall, "covered_wall_triangles": covered,
                          "layer_tets": int(len(tets))}


def _overlap_fraction(grid, tet_volume: float) -> float | None:
    """How much the tetrahedra's summed volume exceeds what the mesh's own boundary triangles
    enclose (0 = they tile it exactly); None when there is no closed boundary to measure."""
    try:
        tris = grid.extract_cells_by_type(5).extract_surface().triangulate()
        if tris.n_cells == 0:
            return None
        # vmtk winds its caps the opposite way from the wall: measured as written, the caps
        # cancel part of the wall in the divergence sum and a sound mesh reads as 4-5% over
        # (bend_elbow_003, tee_wye_003, s_duct_001 - all exactly 0.0% once oriented)
        tris = tris.compute_normals(auto_orient_normals=True, consistent_normals=True,
                                    cell_normals=True, point_normals=False)
        enclosed = abs(float(tris.volume))
    except Exception:  # noqa: BLE001 - evidence, never a crash
        return None
    if enclosed <= 0.0:
        return None
    return tet_volume / enclosed - 1.0



def _run_policy() -> RunPolicy:
    from meshpipeline.engines.vmtk.spec import SPEC

    policy = SPEC.run_policy
    if policy is None:
        raise RuntimeError(
            "the vmtk engine spec has no run_policy; its run gate cannot phrase a verdict "
            "without one")
    return policy


# run_mesh guidance (spec._load_run_enricher)

def run_enricher(R, workspace, res: dict, q: dict, out: dict) -> None:
    pol = _run_policy()
    fatal = out.get("fatal_defects") or []
    if out.get("success"):
        out["guidance"] = pol.ok_guidance
        return
    staged = (Path(workspace) / "vmtk_staging.json").exists() if workspace else False
    if staged and any(("self-intersect" in f.lower()) or ("invalid plc" in f.lower())
                      or ("incomplete" in f.lower()) for f in fatal):
        out["guidance"] = (
            "TetGen did not complete the fill even after the engine's own repair ladder (thinner "
            "layers, then no layers) - the staged wall itself is clean. ONE move per attempt: "
            "nudge edge_length_factor (0.3 -> 0.35, then 0.25) so the surface remesh lands "
            "differently at the junctions; keep everything else as staged. If that fails twice, "
            "report the failure - do not permute other fields.")
        return
    if any(("self-intersect" in f.lower()) or ("invalid plc" in f.lower()) for f in fatal):
        out["guidance"] = (
            "STOP - do not reconfigure. The INPUT lumen surface self-intersects, so TetGen "
            "refuses to bound a volume ('Invalid PLC'); vmtk still exits 0 and leaves inverted "
            "tets. No strategy change - edge length, boundary layers, or capping - can fix a "
            "geometry defect. The surface must be repaired or replaced upstream; report the "
            "geometry as unmeshable rather than retrying the same surface.")
        return
    if any("inverted" in f.lower() or "non-positive volume" in f.lower() for f in fatal):
        # REPAIR LADDER (live aorta run: the planner mutated unrelated knobs and one strategy
        # crashed the mesher). Inverted tets on a NON-self-intersecting lumen are almost
        # always the boundary layer folding into itself: the ONE move is dropping layers.
        out["guidance"] = (
            "INVERTED TETS with a clean input surface: this is the boundary layer folding "
            "into itself, not a sizing problem. Repair ladder, ONE step per attempt, keeping "
            "seeds and every other field UNCHANGED: (1) set boundary_layers=0 and re-run - a "
            "plain radius-adaptive fill is a valid deliverable; (2) only if a LAYER-FREE mesh "
            "still shows inverted tets, reduce edge_length_factor one step (e.g. 0.3 -> 0.2). "
            "Do NOT change seeds, capping, or remeshing in response to this defect.")
        return
    if any("truncated" in f.lower() or "corrupt" in f.lower() for f in fatal)             or (out.get("rc") not in (0, None)):
        out["guidance"] = (
            "The mesher CRASHED on this strategy (nonzero exit / truncated output). Simplify "
            "toward the known-good configuration rather than exploring: boundary_layers=0, "
            "remesh_surface=true, cap_openings=true, seeds UNCHANGED. If the simplest "
            "configuration also crashes, report the failure - do not keep permuting fields.")
        return
    out["guidance"] = f"Not valid ({fatal or pol.fail_label}). {pol.fail_hint}"


# finalize: manifest (executor seam)

def finalize(workspace_dir: str, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    ws = Path(workspace_dir)
    if not (ws / _MESH).exists():
        return {"success": False, "stdout": "", "stderr": "",
                "output": f"[VMTK] no {_MESH} - the Builder did not produce a volume mesh"}
    q = check_mesh(ws)
    # ARTIFACT reconciliation input: the ACTUAL boundary structure of the produced .vtu
    # (CellEntityIds -> wall + cap_N), plus the openings the strategy PROMISED to cap.
    # The manifest's patch_types otherwise echo the intake declaration (circular) - the
    # gate needs real output facts to reconcile against.
    try:
        import json as _json

        from meshpipeline.engines.vmtk.viewer_surface import surface_patches
        _actual = sorted(surface_patches(ws).keys())
        q["actual_boundaries"] = _actual
        _spec = {}
        if (ws / "vmtk_spec.json").exists():
            _spec = _json.loads((ws / "vmtk_spec.json").read_text())
        if _spec.get("cap_openings", True):
            # openings the strategy seeds (and therefore caps): id-mode counts ids,
            # point-mode counts (x,y,z) triples
            _n_src = len(_spec.get("source_ids") or []) or len(_spec.get("source_points") or []) // 3
            _n_tgt = len(_spec.get("target_ids") or []) or len(_spec.get("target_points") or []) // 3
            if _n_src + _n_tgt:
                q["expected_caps"] = _n_src + _n_tgt
        from meshpipeline.engines.vmtk.lumen_staging import read_staging as _read_staging
        _staged = _read_staging(ws)
        if _staged and _staged.get("ports"):
            # the engine opened exactly these ports; each must come back as one cap
            q["expected_caps"] = len(_staged["ports"])
    except Exception:
        logger.warning("vmtk finalize: actual-boundary extraction failed (non-fatal)")
    # the contracted patches: the lumen wall + each capped opening (roles come from intake)
    patch_types = {str(p.get("name")): str(p.get("type"))
                   for p in (intake_patches or []) if p.get("name")}
    # THE REVIEW SURFACE. The reviewer requires mesh_paths.surface (a gmsh .msh, one physical
    # group per patch - engines/vmtk/_shared.py) and refused every vmtk delivery without it
    # ("required artifact 'mesh_paths.surface' does not exist", straight_reducer_014, job
    # 98ec197e, 2026-09-11). Written with the writer the OpenFOAM engines use, from the
    # delivered boundary under the DECLARED names (caps bound to their ports); cap_0 is the
    # tets' own outer faces, the same surface again, and is left out.
    patch_entities: dict = {n: [] for n in patch_types}
    try:
        from meshpipeline.engines.vmtk.viewer_surface import surface_patches as _named_patches
        from meshpipeline.render.review_artifacts import build_review_msh
        _review = {n: t for n, t in _named_patches(ws, named=True).items() if n != "cap_0"}
        if _review:
            _ents, _ = build_review_msh(ws, _review)
            patch_entities.update(_ents)
    except Exception:  # noqa: BLE001 - the mesh stands; the review surface is evidence
        logger.exception("vmtk finalize: review surface (mesh.msh) not written")
    # PREPARED geometry - the staged lumen surface - kept as preparation evidence (body_bbox).
    surf = _read_surface(ws / _LUMEN).extract_surface() if (ws / _LUMEN).exists() else None
    b = surf.bounds if surf is not None else (0, 1, 0, 1, 0, 1)
    # THE MESHED EXTENT: what the finished volume actually occupies, measured on mesh.vtu. The
    # staged surface is close but not the same - the remeshed volume sits a little inside it - and
    # geometry.box_* is defined as the actual meshed extent, so it is read from the mesh.
    _mesh_path = ws / _MESH
    if _mesh_path.exists():
        _mb = _read_surface(_mesh_path).bounds
        bbox = (_mb[0], _mb[2], _mb[4], _mb[1], _mb[3], _mb[5])
    else:
        bbox = (b[0], b[2], b[4], b[1], b[3], b[5])
    from meshpipeline.engines.manifest import write_manifest
    write_manifest(
        ws,
        patch_types=patch_types,
        patch_entities=patch_entities,
        bbox=bbox,
        quality=q,
        domain=domain or "internal flow through a lumen",
        body_bbox=((b[0], b[2], b[4]), (b[1], b[3], b[5])),
        mesh_bounds=bbox,
        volume_path=str((ws / _MESH).resolve()),
        mesh_units=COMPLETED_MESH_UNIT.value,
        mesh_mode="vmtk",
        flow_topology=flow_topology,
        engine_params=engine_params or {},
    )
    out = (f"[VMTK] cells={q['cells']} min_quality={q.get('min_quality')} "
           f"layers={q.get('layer_coverage')} fatal={q.get('fatal', [])}")
    return {"success": q["mesh_ok"], "stdout": out, "stderr": "", "output": out}
