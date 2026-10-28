# Responsibility: Carry every input the conjugate mesher needs from the host that chose them to the container that runs it.
# Owns: the plan file's name and shape, the host-side sizing, and rebuilding the port plan inside the container.
# Boundaries: it decides sizes and ports; it meshes nothing and judges nothing.
# Collaborates with: cfmesh_conjugate/conjugate.py (which it feeds) and engines/dispatch.py (which carries only a workspace).
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: THE SEAM. engines/dispatch.py calls a bundle's runner with (workspace, bashrc, timeout) and
#: nothing else, while conjugate.build needs a cell size, a refinement depth, the coolant's
#: expected volume and piece count, a tolerance, the narrowest passage and a port plan. None of
#: those can be recomputed in the container - the geometry they come from never goes there - so
#: they are chosen on the host, written here, and read back on the other side. Same split as
#: snapgrid's: the driver chooses, the runner reads.
PLAN_NAME = "conjugate_plan.json"
#: What the mesher reads. The block is written by the driver from the part's own bounds; the
#: coolant is the surface the extractor wrote (cad/fluid_volume.write_surface).
BLOCK_STL = "constant/triSurface/block.stl"
FLUID_STL = "constant/triSurface/fluid.stl"
#: The thin side of a two-scale part's wall, written beside the coolant surface by
#: cad/fluid_volume.write_surface (FINE_SURFACE_NAME). Present only for a part that has two scales;
#: it has to travel in the native payload, because a surface the container cannot open is a
#: refinement that silently does not happen.
FLUID_FINE_STL = "constant/triSurface/fluid_fine.stl"
#: What the run leaves for finalize.
REPORT_NAME = "conjugate_report.json"

#: How much wider than the measured staircase error the volume check is allowed to be. The
#: staircase error IS the expected disagreement between a cut-cell mesh and the CAD it came from,
#: so the tolerance has to admit it and little else; 1.5x leaves room for the part's own shape
#: without admitting a mesh that tagged the wrong cells.
VOLUME_TOL_MARGIN = 1.5


@dataclass(frozen=True)
class ConjugatePlan:
    """Every number the container needs, in METRES, and the ports by block-face name.

    METRES END TO END, said here because the one slip this file could carry is a unit. The
    devtools command line takes millimetres and converts at its edge, and when it once passed a
    lid thickness straight through, `--lid 0.1` meant to add 100 um of metal and added 100 mm:
    "turning a 1.3 mm plate into a 101.3 mm one. Nothing failed. The mesh built, every check
    passed, and the block was eighty times the part." A factor of a thousand, silently.
    """

    background_cell_m: float
    levels: int
    expect_fluid_volume_m3: float
    expect_fluid_pieces: int
    volume_tol: float
    narrowest_m: float
    inlet: str = ""
    outlet: str = ""
    port_walls: tuple[str, ...] = ()
    port_reason: str = ""
    thickness_m: float | None = None
    #: What the user called each block face, as render_meshdict's patch_names wants it:
    #: {"ymin": ["coolant_in", "inlet"]}. Empty means the block keeps its own six face names.
    patch_names: dict[str, list[str]] | None = None
    #: Only to say what was asked for, in the report. The cell size above is what is obeyed.
    cells_across: int = 0
    #: The block the mesh fills, in metres. CARRIED RATHER THAN RE-MEASURED: it is the mesh's own
    #: extent and finalize has to publish it, and the alternative - reading it back off the block
    #: STL or the delivered points - is a second answer to a question the host already settled.
    block_lo: tuple[float, float, float] | None = None
    block_hi: tuple[float, float, float] | None = None
    #: Boxes that get their own, finer cell size, as render_meshdict's objectRefinements wants
    #: them: [{"name", "lo", "hi", "cell_m"}] in METRES. Empty means one cell size everywhere.
    #:
    #: WHY A PART NEEDS TWO SIZES. A Corintis cold plate is a manifold of ~3.5 mm channels bonded
    #: to a layer of ~98 um slots. One size that suits the slots prices the manifold's whole
    #: wetted area at the slot's cell - 60,588 mm2 of it - and the estimate reaches three billion
    #: cells. One that suits the manifold cannot see the slots at all: at 0.0947 mm the manifold
    #: got 28 cells across and the slots got one, and the coolant came back in 367 disconnected
    #: pieces. The budget goes where the part is thin, which is what these boxes are for.
    fine_zones: tuple[dict, ...] = ()
    #: Extra surfaces, each a SUBSET of the coolant's triangles, that get more refinement levels
    #: than the coolant as a whole: [{"name", "file", "levels", "thickness_m"}]. This is the
    #: mechanism to reach for, not fine_zones - a band round the thin walls costs half what
    #: filling their bounding box costs, measured 224,431,537 cells against 456,206,329 on the
    #: first customer part. cad/scales.py decides where the thin walls are and writes the file.
    fine_surfaces: tuple[dict, ...] = ()


def background_for(narrowest_m: float, cells_across: int, levels: int) -> float:
    """The BACKGROUND cell that puts `cells_across` cells across the narrowest passage.

    The octree halves the cell once per level, so the background has to start 2**levels coarser
    than the cell wanted at the coolant surface. Asking for the interface size and handing it over
    as the background is the mistake that produces a mesh 512 times too big.
    """
    if cells_across < 1:
        raise ValueError(f"cells across must be at least 1, got {cells_across}")
    if narrowest_m <= 0.0:
        raise ValueError(f"the narrowest passage must be positive, got {narrowest_m}")
    return (float(narrowest_m) / int(cells_across)) * (2 ** int(levels))


def write_plan(workspace, plan: ConjugatePlan) -> None:
    """The plan beside the case, NOT under constant/.

    foam_case_guard scans everything under constant/ as authored OpenFOAM dictionaries, and a JSON
    object there is neither a dictionary it can read nor something it should have to judge.
    """
    Path(workspace, PLAN_NAME).write_text(
        json.dumps(asdict(plan), sort_keys=True), encoding="utf-8")


def read_plan(workspace) -> ConjugatePlan:
    """The plan the host wrote. Raises ValueError rather than guessing at anything missing.

    NOTHING IS DEFAULTED HERE, and the cell size is why: it is keyword-only with no default on
    build() because there is no size that is right for an unknown part, and a plan that silently
    acquired one would mesh at a resolution nobody chose and report a cell count nobody can
    account for.
    """
    p = Path(workspace, PLAN_NAME)
    if not p.is_file():
        raise ValueError(f"no {PLAN_NAME} in the workspace: the conjugate mesher cannot choose a "
                         "cell size, a tolerance or a port plan for itself")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{PLAN_NAME} is not a JSON object")
    missing = [k for k in ("background_cell_m", "expect_fluid_volume_m3", "narrowest_m")
               if data.get(k) is None]
    if missing:
        raise ValueError(f"{PLAN_NAME} is missing {missing}, which cannot be defaulted")
    known = {f for f in ConjugatePlan.__dataclass_fields__}
    out = {k: v for k, v in data.items() if k in known}
    if out.get("port_walls") is not None:
        out["port_walls"] = tuple(out["port_walls"])
    for k in ("block_lo", "block_hi"):
        got = out.get(k)
        if got is not None:
            if len(got) != 3:
                raise ValueError(f"{PLAN_NAME}: {k} is not three numbers")
            out[k] = (float(got[0]), float(got[1]), float(got[2]))
    if out.get("fine_zones") is not None:
        zones = []
        for n, z in enumerate(out["fine_zones"]):
            if not isinstance(z, dict):
                raise ValueError(f"{PLAN_NAME}: refinement zone {n} is not an object")
            lo, hi, cell = z.get("lo"), z.get("hi"), z.get("cell_m")
            if lo is None or hi is None or cell is None:
                raise ValueError(f"{PLAN_NAME}: refinement zone {n} needs lo, hi and cell_m")
            if len(lo) != 3 or len(hi) != 3:
                raise ValueError(f"{PLAN_NAME}: refinement zone {n} is not a box in three axes")
            if float(cell) <= 0.0:
                raise ValueError(f"{PLAN_NAME}: refinement zone {n} has cell_m {cell}, which is "
                                 "not a cell size")
            if any(float(hi[j]) <= float(lo[j]) for j in range(3)):
                raise ValueError(f"{PLAN_NAME}: refinement zone {n} has no extent: {lo} .. {hi}")
            zones.append({"name": str(z.get("name") or f"fine{n}"),
                          "lo": (float(lo[0]), float(lo[1]), float(lo[2])),
                          "hi": (float(hi[0]), float(hi[1]), float(hi[2])),
                          "cell_m": float(cell)})
        out["fine_zones"] = tuple(zones)
    if out.get("fine_surfaces") is not None:
        fines = []
        for n, s in enumerate(out["fine_surfaces"]):
            if not isinstance(s, dict):
                raise ValueError(f"{PLAN_NAME}: fine surface {n} is not an object")
            f_file, f_levels = s.get("file"), s.get("levels")
            if not f_file or f_levels is None:
                raise ValueError(f"{PLAN_NAME}: fine surface {n} needs file and levels")
            if int(f_levels) <= 0:
                raise ValueError(f"{PLAN_NAME}: fine surface {n} asks for {f_levels} levels")
            fines.append({"name": str(s.get("name") or f"fine{n}"), "file": str(f_file),
                          "levels": int(f_levels),
                          "thickness_m": (None if s.get("thickness_m") is None
                                          else float(s["thickness_m"]))})
        out["fine_surfaces"] = tuple(fines)
    return ConjugatePlan(**out)


def ports_from(plan: ConjugatePlan):
    """The port plan as the mesher's own frozen type, or None when the part is sealed.

    A sealed part is still worth meshing - the branching test part is the hardest topology in the
    set and has no ports at all - so no inlet is a legal plan and not an error.
    """
    from meshpipeline.engines.cfmesh_conjugate.conjugate import PortPlan

    if not plan.inlet and not plan.outlet:
        return None
    return PortPlan(inlet=plan.inlet, outlet=plan.outlet, walls=tuple(plan.port_walls),
                    reason=plan.port_reason or "carried in the plan")


def plan_for(*, narrowest_m: float, fluid_volume_m3: float, passages: int,
             solid_lo, solid_hi, fluid_lo, fluid_hi, cells_across: int, levels: int,
             patch_names: dict | None = None, lid_m: float = 0.0,
             inlet: str | None = None, outlet: str | None = None,
             no_ports: bool = False) -> tuple[ConjugatePlan, tuple, tuple]:
    """Everything derived on the host, before anything runs: (plan, block lo, block hi).

    The same sequence devtools/semiconductor/semi.py has always used, lifted so the platform and
    the command line cannot drift apart: the block from the solid's own bounds, the cell size from
    the cells asked for across the narrowest passage, the open faces from where the coolant
    reaches the block, and the ports from the passage's own extent.

    THE TOLERANCE IS COMPUTED HERE AND STORED, never recomputed in the container. It comes from
    the staircase error measured for this resolution, and if the container worked it out again
    from its own idea of the resolution, the gate and the delivered evidence could disagree about
    whether the same mesh passed.
    """
    from meshpipeline.engines.cfmesh_conjugate import conjugate as C

    grow = {"zmax": float(lid_m)} if lid_m else None
    lo, hi = C.block_bounds(solid_lo, solid_hi, grow)
    background = background_for(narrowest_m, cells_across, levels)
    open_ = C.open_faces(fluid_lo, fluid_hi, lo, hi)
    extent = tuple(fluid_hi[k] - fluid_lo[k] for k in range(3))
    ports = None if no_ports else C.plan_ports(open_, extent, inlet=inlet, outlet=outlet)
    plan = ConjugatePlan(
        background_cell_m=float(background), levels=int(levels),
        expect_fluid_volume_m3=float(fluid_volume_m3), expect_fluid_pieces=int(passages),
        volume_tol=_tolerance(cells_across), narrowest_m=float(narrowest_m),
        inlet=(ports.inlet if ports else ""), outlet=(ports.outlet if ports else ""),
        port_walls=(tuple(ports.walls) if ports else ()),
        port_reason=(ports.reason if ports else ""),
        thickness_m=None, patch_names=_as_lists(patch_names), cells_across=int(cells_across),
        block_lo=(float(lo[0]), float(lo[1]), float(lo[2])),
        block_hi=(float(hi[0]), float(hi[1]), float(hi[2])))
    return plan, lo, hi


def _as_lists(patch_names: dict | None) -> dict[str, list[str]] | None:
    """{"ymin": ("coolant_in", "inlet")} as JSON can hold it."""
    if not patch_names:
        return None
    return {str(face): [str(name), str(role)] for face, (name, role) in patch_names.items()}


def as_patch_names(plan: ConjugatePlan) -> dict[str, tuple[str, str]] | None:
    """...and back, as render_meshdict's patch_names wants it."""
    if not plan.patch_names:
        return None
    return {face: (str(pair[0]), str(pair[1]))
            for face, pair in plan.patch_names.items() if len(pair) >= 2}


def _tolerance(cells_across: int) -> float:
    """How far the meshed coolant may be from the CAD, for this resolution.

    The staircase error is the expected disagreement, so the bar is that error with a margin,
    never tighter than the mesher's own default - a coarse run disagreeing by more than its own
    staircase error has tagged the wrong cells, which is what this is for.
    """
    from meshpipeline.engines.cfmesh_conjugate.conjugate import DEFAULT_VOLUME_TOL

    try:
        from meshpipeline.engines.staircase_error import single_passage_error_pct
        pct = float(single_passage_error_pct(int(cells_across)))
    except Exception:                      # noqa: BLE001 - the default is a safe floor
        logger.info("conjugate plan: no staircase measurement for %s cells across; "
                    "the default tolerance stands", cells_across)
        return DEFAULT_VOLUME_TOL
    return max(DEFAULT_VOLUME_TOL, VOLUME_TOL_MARGIN * pct / 100.0)
