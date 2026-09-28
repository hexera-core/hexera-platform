# Responsibility: Fuzz the whole road a declared boundary travels - the intake tool boundary, validation, admission, the preview token, the approved snapshot and dispatch re-check, the builder workspace, each engine's own case writer, the pre-flight, and the gates on a mesh built to the written case - and hold one rule: what the user approved is what gets delivered, or it is refused at intake with the fix named.
# Boundaries: pure Python, seconds long. No OpenFOAM, gmsh or VMTK binary runs: the native mesher is replaced by one that builds exactly the boundary the WRITTEN case asks for, and nothing that decides a patch's name or type is stubbed. Geometry is synthetic boxes; geometry-only steps (symmetry-plane detection, CAD tessellation, thin-feature measurement, checkMesh) are stood in for.
from __future__ import annotations

import asyncio
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests._geometry_support import geometry_state, prepared_surface

import meshpipeline.settings.policy as polcfg
from meshpipeline.agents.intake import admission_token as at
from meshpipeline.agents.intake.executor import (
    IntakeExecutionState,
    IntakeToolExecutor,
    IntakeToolResult,
)
from meshpipeline.agents.intake.validation import (
    ADMIT_SUPPORTED,
    normalise_submission,
    preview_admission,
    validate_submission,
)
from meshpipeline.application.approved_patch_contract import (
    ApprovedPatchContract,
    check_admission,
)
from meshpipeline.cad.stl_io import _box_triangles, _write_solid
from meshpipeline.contracts import mesh_execution
from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
from meshpipeline.contracts.patch_names import _word as _typed_word
from meshpipeline.engines import case_contract
from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.purposes import PURPOSES, is_compatible, topology_of
from meshpipeline.engines.registry import engine_names, get_spec
from meshpipeline.engines.workspace_facts import contract_patches, contract_wall_patch

SEED = 20260928
FUZZ_CASES = 480
#: FUZZ_CANONICAL_TYPES=1 draws only the role words themselves - for running this against code
#: that predates the type vocabulary, where every synonym would stop at intake and hide what lies
#: behind it
_CANONICAL_TYPES = bool(__import__("os").environ.get("FUZZ_CANONICAL_TYPES"))

_REQUEST = ("A complete requirements summary: the uploaded geometry, the simulation type and "
            "purpose, every confirmed parameter, and the mesh requirements, with sizing left to "
            "the builder's engineering discretion. ")
_BRIEF = ("Acceptance: a valid mesh with no fatal defects, every declared boundary present with "
          "its declared type, and sizing at the builder's discretion. ")

# the words people type

NAMES: dict[str, list[str]] = {
    "wall": ["car wall", "Car Body", "wing", "fuselage-main", "body.surface", "Außenhaut",
             "2nd wing", "WALL", "wall", "hull (port side)", "car-wall", "car_wall", "Wing",
             "pipe wall", "lumen", "duct_wall"],
    "farfield": ["farfield", "far field", "Far-Field", "freestream", "outer boundary", "domain",
                 "FARFIELD", "free stream"],
    "ground": ["ground", "Ground", "ground plane", "floor", "road", "Road Surface",
               "moving ground", "ground_plane"],
    "symmetry": ["symmetry", "sym plane", "Symmetry-Y", "mid plane", "mirror"],
    "empty": ["frontAndBack", "front and back", "empty sides"],
    "inlet": ["inlet", "Inlet-1", "inlet 2", "2nd inlet", "feed", "Einlass", "intake.port",
              "INLET", "supply"],
    "outlet": ["outlet", "outlet 1", "Outlet-B", "exhaust", "drain", "3rd outlet", "Outlet"],
    "fixed": ["fixed support", "bolt hole 1", "Fixed", "clamp face", "base", "Bolt-Hole 1"],
    "load": ["load face", "bolt hole 2", "pressure face", "Load-1", "top"],
    "contact": ["contact pad", "Contact"],
    "free": ["free surface", "rest"],
    "external": ["outer shell", "heater face"],
}
#: names a mesher keeps for itself, and one that means a role it is not
RESERVED = ["outer", "FoamFile", "defaultFaces", "fixedWalls", "solid", "seedZone", "thinZone3",
            "Outer"]
TYPES: dict[str, list[str]] = {
    "wall": ["wall", "Wall", "no-slip wall", "no slip", "solid wall", "WALL", "noslip"],
    "farfield": ["farfield", "far field", "Far-Field", "freestream", "outer boundary", "opening"],
    "ground": ["wall", "ground", "ground plane", "floor", "road"],
    "symmetry": ["symmetry", "symmetry plane", "symmetryPlane", "mirror"],
    "empty": ["empty", "front and back"],
    "inlet": ["inlet", "velocity inlet", "Velocity-Inlet", "mass flow inlet", "inflow"],
    "outlet": ["outlet", "pressure outlet", "outflow", "exit"],
    "fixed": ["fixed", "fixed support", "clamped", "encastre"],
    "load": ["load", "force", "pressure", "applied load"],
    "contact": ["contact", "bonded"],
    "free": ["free", "free surface"],
    "external": ["external"],
}
#: a word with two meanings inside a duct - the intake must ask, not guess
AMBIGUOUS_INTERNAL = ["opening", "pressure"]
PORT_DIAMETERS_MM = [12.0, 25.0, 50.0, 100.0, 200.0]    # area ratio 4: always told apart


@dataclass
class Scenario:
    label: str
    engine: str
    purpose: str
    input_kind: str
    dimensionality: str
    patches: list[dict]
    upload: str = "stl"                       # stl | stl-multi | step | step-assembly | lumen
    parts: list[str] = field(default_factory=list)   # the upload's named solids / CAD parts
    engine_params: dict = field(default_factory=dict)
    #: the one thing the generator KNOWS must be refused at intake, or ""
    must_refuse: str = ""
    features: set[str] = field(default_factory=set)


def _engine_params(engine: str) -> dict:
    return {p.key: p.default for p in get_spec(engine).intake_params}


def _combos() -> list[tuple[str, str, str, str]]:
    """Every (engine, purpose, input_kind, dimensionality) the registry admits."""
    out = []
    for engine in engine_names():
        spec = get_spec(engine)
        for purpose in PURPOSES:
            if not is_compatible(spec, purpose):
                continue
            kinds = sorted({c.input_kind for c in spec.capabilities
                            if is_compatible(spec, purpose, c.input_kind)})
            dims = spec.input_contract.dimensionalities if spec.input_contract else ("3D",)
            for ik in kinds:
                for dim in dims:
                    if ik == "planar-domain" and dim != "2D":
                        continue
                    if dim == "2D" and purpose == "structural" and ik != "planar-domain":
                        continue
                    out.append((engine, purpose, ik, dim))
    return out


def _pick(rng: random.Random, pool: list[str], used: set[str]) -> str:
    free = [n for n in pool if n not in used] or pool
    name = rng.choice(free)
    used.add(name)
    return name


def generate(rng: random.Random, combo) -> Scenario:
    engine, purpose, ik, dim = combo
    used: set[str] = set()
    patches: list[dict] = []
    sc = Scenario(label="", engine=engine, purpose=purpose, input_kind=ik, dimensionality=dim,
                  patches=patches, engine_params=_engine_params(engine))
    topo = topology_of(purpose)

    def add(role: str, *, name: str | None = None, typ: str | None = None, **extra) -> dict:
        word = rng.choice(TYPES[role])
        if _CANONICAL_TYPES:
            word = "wall" if role == "ground" else role
        p = {"name": name or _pick(rng, NAMES[role], used), "type": typ or word, **extra}
        patches.append(p)
        return p

    if purpose == "external_cfd":
        walls = rng.choices([1, 2, 3], weights=[7, 2, 1])[0]
        wall_names = [_pick(rng, NAMES["wall"], used) for _ in range(walls)]
        for n in wall_names:
            add("wall", name=n)
        if walls > 1:
            sc.features.add("multi_wall")
        if engine in ("snappy", "cfmesh"):
            sc.upload = rng.choice(["stl", "stl", "stl-multi", "step-assembly"])
            if sc.upload == "stl-multi":
                # the file names its parts; sometimes exactly as typed, sometimes in its own capitals
                sc.parts = [rng.choice([n, n.upper(), n.replace(" ", "_")]) for n in wall_names]
                if walls == 1:
                    sc.parts = sc.parts + ["wheels", "mirrors"]
            elif sc.upload == "step-assembly":
                sc.parts = list(wall_names) + (["wheels"] if walls == 1 else [])
                if walls > 1:
                    sc.must_refuse = "cad parts are flattened by this engine"
        else:
            sc.upload = "step"
        if rng.random() < 0.3 and engine in ("snappy", "cfmesh") and dim == "3D":
            add("ground")
            sc.features.add("ground")
            if engine == "cfmesh":
                sc.must_refuse = sc.must_refuse or "cfMesh lays no ground plane"
        farfields = rng.choices([1, 2], weights=[85, 15])[0]
        for _ in range(farfields):
            add("farfield")
        if farfields == 2 and engine in ("snappy", "cfmesh"):
            sc.must_refuse = sc.must_refuse or "one far-field box is one patch"
        if engine == "snappy" and rng.random() < 0.15:
            for _ in range(rng.choice([1, 2])):
                add("symmetry")
            sc.features.add("symmetry")
        if dim == "2D":
            empties = rng.choices([1, 2], weights=[80, 20])[0]
            for _ in range(empties):
                add("empty")
            if empties == 2 and engine == "cfmesh":
                sc.must_refuse = sc.must_refuse or "cfMesh 2D merges front and back into one"
    elif purpose == "internal_cfd":
        sc.upload = "lumen" if engine == "vmtk" else "step"
        walls = rng.choices([1, 2], weights=[85, 15])[0]
        for _ in range(walls):
            add("wall")
        if rng.random() < 0.1:
            add("ground", typ=rng.choice(["wall", "floor"]))       # inside a duct: just a wall
            walls += 1
        if walls > 1 and engine in ("snappy", "cfmesh", "vmtk"):
            sc.must_refuse = "the carve delivers one wall"
        sizes = rng.sample(PORT_DIAMETERS_MM, k=rng.randint(2, 4))
        n_in = rng.randint(1, len(sizes) - 1)
        for i, d in enumerate(sizes):
            role = "inlet" if i < n_in else "outlet"
            add(role, diameter_mm=d)
        if rng.random() < 0.1:
            patches[-1]["type"] = rng.choice(AMBIGUOUS_INTERNAL)
            sc.must_refuse = sc.must_refuse or "an ambiguous boundary word is a question"
        if rng.random() < 0.08 and engine in ("snappy", "cfmesh", "vmtk"):
            add("symmetry")
            sc.must_refuse = sc.must_refuse or "no symmetry plane inside a carved duct"
        if dim == "2D":
            add("empty")
            if engine == "cfmesh":
                sc.must_refuse = sc.must_refuse or "cfMesh has no 2D internal path"
    elif purpose == "structural":
        sc.upload = "step"
        for role, k in (("fixed", rng.randint(1, 2)), ("load", rng.randint(1, 3)),
                        ("contact", rng.randint(0, 1)), ("free", rng.randint(0, 1))):
            for _ in range(k):
                add(role)
    elif purpose == "conjugate_heat_transfer":
        sc.upload = "step"
        for role in ("inlet", "outlet", "wall", "external"):
            add(role)
    # noise every purpose sees: a reserved name, two different names that meet once cleaned, and
    # the same name twice in other capitals - one boundary declared twice, which must be asked
    # about, never quietly turned into two
    if patches and rng.random() < 0.12:
        victim = rng.choice(patches)
        victim["name"] = rng.choice(RESERVED)
        sc.features.add("reserved")
    if len(patches) > 1 and rng.random() < 0.08:
        a, b = rng.sample(patches, 2)
        b["name"] = a["name"].replace(" ", "-") if " " in a["name"] else a["name"] + "."
        sc.features.add("meet_after_cleaning")
    if len(patches) > 1 and rng.random() < 0.06:
        a, b = rng.sample(patches, 2)
        b["name"] = a["name"].swapcase() if a["name"].swapcase() != a["name"] else a["name"]
        sc.must_refuse = sc.must_refuse or "the same name twice is a question"
        sc.features.add("capitals")
    sc.label = f"{engine}/{purpose}/{ik}/{dim}"
    return sc


# the intake boundary: the executor's real run(), with the handlers' own work captured

def _intake_args(sc: Scenario, patches) -> dict:
    args = {"domain": "fuzzed case", "request_txt": _REQUEST * 2, "review_brief_txt": _BRIEF * 2,
            "mesh_engine": sc.engine, "engine_source": "user_direct", "purpose": sc.purpose,
            "input_kind": sc.input_kind, "dimensionality": sc.dimensionality,
            "engine_params": dict(sc.engine_params), "patches": json.loads(json.dumps(patches)),
            "preview_token": "t"}
    if topology_of(sc.purpose) == "external":
        args["flow_axis"] = "+x"
    return args


def through_intake(sc: Scenario) -> tuple[dict, dict, str]:
    """(the preview's args, the submission's args, the tool result the model reads) after the
    executor's boundary - exactly what every handler, the token and the snapshot receive."""
    seen: dict = {}
    ex = IntakeToolExecutor(state=IntakeExecutionState(session_id="s", owner_id="o"),
                            job_id="j", implemented_engines=list(engine_names()),
                            search_tool=None, trace=None)

    def _capture(tool):
        async def _h(args):
            seen[tool] = args
            return IntakeToolResult(tool=tool, accepted=True, content="ok")
        return _h

    ex._do_preview_selected_admission = _capture("preview")        # type: ignore[method-assign]
    ex._do_submit_requirements = _capture("submit")                # type: ignore[method-assign]
    sub_args = _intake_args(sc, sc.patches)
    pre_args = {"selected_engine": sc.engine, "purpose": sc.purpose, "input_kind": sc.input_kind,
                "dimensionality": sc.dimensionality, "engine_params": dict(sc.engine_params),
                "patches": json.loads(json.dumps(sc.patches))}
    asyncio.run(ex.run("preview_selected_admission", pre_args))
    res = asyncio.run(ex.run("submit_requirements", sub_args))
    return seen["preview"], seen["submit"], res.content


# the upload and what the intake reads off it

def _write_parts(path: Path, parts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        if not parts:
            _write_solid(fh, "body", _box_triangles([0.0, 0.0, 0.0], [1.0, 0.4, 0.3]))
        for i, name in enumerate(parts):
            x0 = 1.2 * i
            _write_solid(fh, name, _box_triangles([x0, 0.0, 0.0], [x0 + 1.0, 0.4, 0.3]))


def geometry_facts(sc: Scenario, upload: Path) -> dict:
    """What the intake's _geometry_facts would read for this upload: the real reader for an STL,
    and the facts cad/regions.py reports for a CAD file (reading one needs OpenCASCADE)."""
    from meshpipeline.cad.regions import regions_of
    if sc.upload in ("stl", "stl-multi"):
        return regions_of(upload).as_facts()
    if sc.upload == "step-assembly" and len(set(sc.parts)) > 1:
        return {"region_names": list(sc.parts), "region_count": len(sc.parts),
                "region_source": "assembly"}
    return {}


# the native mesher, replaced: it builds exactly the boundary the written case asks for

class FakeMesher:
    def __init__(self):
        self.runs: list[tuple[str, str]] = []

    def run(self, workspace, *, engine: str, timeout: int) -> dict:
        ws = Path(workspace)
        self.runs.append((engine, str(ws)))
        if engine in ("snappy", "cfmesh"):
            case = case_contract.read_case_boundary(ws, engine)
            assert case is not None, f"the {engine} case this system wrote cannot be read back"
            _write_polymesh(ws, case.patches, empty=set(case.engine_owned))
            return {"rc": 0, "timed_out": False, "log_tail": ""}
        if engine == "gmsh":
            spec = json.loads((ws / "gmsh_spec.json").read_text())
            (ws / "mesh.inp").write_text("*NODE\n")
            (ws / "mesh.msh").write_text("$MeshFormat\n")
            (ws / "quality.json").write_text(json.dumps({
                "cells": 500, "nodes": 300, "element_order": 2, "min_sicn": 0.5,
                "sicn_low_fraction": 0.0, "fatal": [], "size_h": 0.01,
                "bounds": [0, 0, 0, 1, 1, 1], "cells_across_min": 20.0,
                "groups": {g["name"]: g["role"] for g in spec["groups"]},
                "default_group": "free", "default_group_used": False}))
            return {"rc": 0, "timed_out": False, "log_tail": ""}
        if engine == "vmtk":
            (ws / "mesh.vtu").write_text("<VTKFile></VTKFile>")
            return {"rc": 0, "timed_out": False, "log_tail": ""}
        raise AssertionError(f"no fake mesher for {engine}")


def _write_polymesh(ws: Path, patches: dict, *, empty: set) -> None:
    (ws / "blockMesh.log").write_text("ok\n")
    pm = ws / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    for f in ("points", "faces", "owner", "neighbour"):
        (pm / f).write_text("FoamFile { }\n1\n(\n0\n)\n")
    blocks, start = [], 0
    for name, typ in patches.items():
        n = 0 if name in empty else 10
        blocks.append(f"    {name}\n    {{\n        type {typ};\n        nFaces {n};\n"
                      f"        startFace {start};\n    }}")
        start += n
    (pm / "boundary").write_text(
        "FoamFile { version 2.0; format ascii; class polyBoundaryMesh; object boundary; }\n"
        f"{len(blocks)}\n(\n" + "\n".join(blocks) + "\n)\n")


_QUALITY = {"cells": 5000, "fatal": [], "skew_fraction": 0.0, "skew_faces": 0,
            "max_skewness": 1.0, "max_non_ortho": 30.0, "mesh_ok": True}


class _Publish:
    async def _any(self, *a, **k):
        return None

    def __getattr__(self, name):
        if name.startswith("a"):
            return self._any
        raise AttributeError(name)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The seams every engine run passes through, pointed at the fake mesher; and the
    geometry-only steps stood in for."""
    import meshpipeline.engines.snappy.drivers as drv
    import meshpipeline.engines.snappy.layer_policy as LP
    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo
    from meshpipeline.engines.cfmesh import cfmesh_runner as C
    from meshpipeline.engines.snappy import snappy_runner as R

    mesher = FakeMesher()
    monkeypatch.setattr(mesh_execution, "_executor", mesher)
    monkeypatch.setattr(mesh_execution, "_launch_check", case_contract.launch_check)

    async def _plan(**_kw):
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"),
                              input_tokens=1, output_tokens=1)
        return planner.PlanOutcome({"approach": "fuzz", "max_cells": 200000}, rr)

    monkeypatch.setattr(planner, "plan_with_accounting", _plan)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(LP, "measure_field", lambda *_a, **_k: None)
    # symmetry DETECTION is geometry; the declared names are what is under test
    monkeypatch.setattr(R, "detect_symmetry_plane", lambda _an, name, **_k: {
        "axis": 1, "pos": 0.0, "side": "min", "name": name})
    monkeypatch.setattr(R, "detect_slab_symmetry", lambda _an, lo, hi, **_k: {
        "slab": True, "axis": 1, "pos_lo": 0.0, "pos_hi": 0.4, "lo_name": lo, "hi_name": hi,
        "cap_frac": 0.1})
    for mod in (R, C):
        monkeypatch.setattr(mod, "check_mesh", lambda *_a, **_k: dict(_QUALITY), raising=False)
        monkeypatch.setattr(mod, "export_volume_vtk", lambda *_a, **_k: None, raising=False)
        monkeypatch.setattr(mod, "build_review_msh",
                            lambda _ws, solids: ({n: [] for n in solids}, (0.0,) * 6),
                            raising=False)
    monkeypatch.setattr(C, "_to_fms", lambda *_a, **_k: "geom.stl")
    monkeypatch.setattr("meshpipeline.engines.surface_deliverable.build_surface_msh",
                        lambda *_a, **_k: None)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded", lambda **kw: {})
    monkeypatch.setattr("meshpipeline.cad.unit_evidence.parser_applied_unit",
                        lambda *_a, **_k: LengthUnit.metre)
    monkeypatch.setattr("meshpipeline.cad.thin_features.thin_refinement_boxes",
                        lambda *_a, **_k: [])
    for fn in ("passage_of_stls", "choose_passage_radius", "port_radius_stats"):
        monkeypatch.setattr(f"meshpipeline.engines.passage.{fn}", lambda *_a, **_k: {})
    return SimpleNamespace(mesher=mesher, root=tmp_path, monkeypatch=monkeypatch)


# the internal carve's measured openings, as the tessellator would report them

def _internal_tessellation(ws: Path, patches: list[dict]):
    stl_dir = ws / "_internal_stls"
    stl_dir.mkdir(parents=True, exist_ok=True)

    def _stl(name, lo, hi):
        p = stl_dir / f"{name}.stl"
        with p.open("w") as fh:
            _write_solid(fh, name, _box_triangles(lo, hi))
        return str(p)

    stls = {"wall": _stl("wall", [0, 0, 0], [1, 1, 1])}
    openings = {}
    ports = [p for p in patches if p.get("type") in ("inlet", "outlet")]
    for i, p in enumerate(ports):
        key = "inlet" if i == 0 else f"outlet_{i}"
        area = math.pi * (float(p["diameter_mm"]) / 2000.0) ** 2
        stls[key] = _stl(key, [0.1 * i, 0, 0], [0.1 * i + 0.05, 0.05, 0.001])
        openings[key] = {"area": area, "centroid": [0.1 * i, 0.0, 0.0]}

    def _tess(*_a, **_k):
        return {"stls": stls, "interior_point": [0.5, 0.5, 0.5], "bbox_min": [0, 0, 0],
                "bbox_max": [1, 1, 1], "openings": openings, "n_wall_faces": 12}
    return _tess


# each engine's real path, from the approved workspace to a built mesh

def _run_snappy(world, sc: Scenario, ws: Path, patches: list[dict]) -> None:
    import meshpipeline.engines.snappy.drivers as drv
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.engines.snappy import snappy_runner as R

    internal = topology_of(sc.purpose) == "internal"
    src = ws.parent / "_src"
    geom = geometry_state(src, unit=LengthUnit.metre,
                          filename="part.step" if internal else "body.stl")
    if internal:
        world.monkeypatch.setattr(R, "tessellate_internal", _internal_tessellation(ws, patches))
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial", deadline_s=600.0)

    async def _fence(*_a, **_k):
        return None
    world.monkeypatch.setattr(run, "fence", _fence)
    state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
             "intake_patches": patches, "dimensionality": sc.dimensionality,
             "flow_topology": topology_of(sc.purpose), "flow_axis": "+x",
             "purpose": sc.purpose, "input_kind": sc.input_kind, "geometry": geom}
    ok, _value, _outcome = asyncio.run(drv.drive(ws, state, job_id="j", publish=_Publish(),
                                                 run=run, source_path=str(geom["local_path"])
                                                 if internal else ""))
    assert ok, f"the snappy driver did not produce a mesh ({sc.label})"


def _run_cfmesh(world, sc: Scenario, ws: Path, patches: list[dict]) -> None:
    from meshpipeline.engines.cfmesh import cfmesh_runner as C
    from meshpipeline.engines.cfmesh.native import run_cartesian_mesh

    if topology_of(sc.purpose) == "internal":
        (ws / "geometry.step").write_text("ISO-10303-21;\n")
        world.monkeypatch.setattr(C, "tessellate_internal", _internal_tessellation(ws, patches))
    # exactly what the builder's configure_mesh tool hands the engine
    wall = contract_wall_patch(ws) or "body"
    out = C.configure_mesh(ws, geometry_file="input.stl", strategy={}, wall_patch=wall,
                           contract_patches=contract_patches(ws), args={},
                           cell_budget=polcfg.CELL_HARD_LIMIT,
                           surface=prepared_surface(ws / "input.stl"))
    assert out.get("success"), f"cfMesh configure_mesh refused an approved case: {out}"
    res = run_cartesian_mesh(ws, timeout=60)
    assert res.get("rc") == 0, res


def _run_gmsh(world, sc: Scenario, ws: Path, patches: list[dict]) -> None:
    from meshpipeline.engines.gmsh import gmsh_runner
    from meshpipeline.engines.gmsh.driver import _validate_spec

    # the builder model authors this spec; a faithful one maps every contracted patch to a group
    tags = "curve_tags" if sc.dimensionality == "2D" else "surface_tags"
    spec = {"dimensionality": sc.dimensionality,
            "groups": [{"name": p["name"], "role": p["type"], tags: [i + 1]}
                       for i, p in enumerate(contract_patches(ws))]}
    assert _validate_spec(spec) == [], _validate_spec(spec)
    (ws / "gmsh_spec.json").write_text(json.dumps(spec))
    res = gmsh_runner.run_cartesian_mesh(ws, timeout=60)
    assert res.get("rc") == 0, res


def _run_vmtk(world, sc: Scenario, ws: Path, patches: list[dict]) -> None:
    from meshpipeline.engines.vmtk import vmtk_runner

    # the engine opens the lumen at the declared ports and records them under the declared names
    ports = [{"name": p["name"], "role": p["type"], "centroid": [0.1 * i, 0, 0], "size_m": 0.02}
             for i, p in enumerate(patches) if p["type"] in ("inlet", "outlet")]
    (ws / "vmtk_staging.json").write_text(json.dumps({"ports": ports}))
    (ws / "vmtk_spec.json").write_text("{}")
    res = vmtk_runner.run_cartesian_mesh(ws, timeout=60)
    assert res.get("rc") == 0, res


RUNNERS = {"snappy": _run_snappy, "cfmesh": _run_cfmesh, "gmsh": _run_gmsh, "vmtk": _run_vmtk}
#: the gates that judge names, types and deliverability - the ones a naming mismatch fails
NAMING_GATES = ("manifest_valid", "patch_contract", "boundary_types")


def finalize_and_gate(sc: Scenario, ws: Path, patches: list[dict]) -> list[str]:
    from meshpipeline.engines.manifest import write_manifest
    from meshpipeline.engines.runtime import get_engine

    topo = topology_of(sc.purpose)
    if sc.engine == "vmtk":
        # vmtk's finalize reads the tetrahedra with pyvista; the manifest it writes is this one:
        # roles from the declaration, the delivered wall + one cap per staged port
        ports = [p for p in patches if p["type"] in ("inlet", "outlet")]
        write_manifest(ws, patch_types={p["name"]: p["type"] for p in patches},
                       patch_entities={p["name"]: [] for p in patches}, bbox=(0, 0, 0, 1, 1, 1),
                       quality={"cells": 500, "fatal": [], "min_quality": 0.5,
                                "actual_boundaries": ["wall"] + [f"cap_{i + 2}"
                                                                 for i in range(len(ports))],
                                "expected_caps": len(ports)},
                       mesh_units=COMPLETED_MESH_UNIT.value, mesh_mode="vmtk",
                       flow_topology=topo)
    else:
        out = get_engine(sc.engine).finalize(str(ws), patches, sc.engine, "fuzz",
                                              topo == "internal", sc.engine_params, topo)
        assert out["success"], out
    ctx = GateCtx(workspace=ws, engine=sc.engine, domain="fuzz", intake_patches=patches,
                  engine_params=sc.engine_params)
    failures = []
    for g in get_spec(sc.engine).gates:
        if g.key in NAMING_GATES:
            ok, feedback = g.check(ctx)
            if not ok:
                failures.append(f"{g.key}: {feedback[:600]}")
    return failures


def _written_text(ws: Path) -> str:
    """Every file a case writer produced, as one text - for the plain check that each approved
    name is written somewhere, independently of the pre-flight's own reading."""
    parts = []
    for rel in ("system/blockMeshDict", "system/snappyHexMeshDict", "system/meshDict",
                "system/createPatchDict", "geom.stl", "gmsh_spec.json", "vmtk_staging.json"):
        f = ws / rel
        if f.exists():
            parts.append(f.read_text(errors="replace"))
    for stl in sorted((ws / "constant" / "triSurface").glob("*.stl")):
        parts.append(stl.name)
        parts.extend(line for line in stl.read_text(errors="replace").splitlines()
                     if line.startswith("solid "))
    return "\n".join(parts)


@dataclass
class Outcome:
    sc: Scenario
    approved: bool = False
    refusals: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    patches: list[dict] = field(default_factory=list)


def walk(world, sc: Scenario, ws: Path) -> Outcome:
    from meshpipeline.agents.builder.workspace import _write_workspace_context_files

    oc = Outcome(sc=sc)
    ws.mkdir(parents=True, exist_ok=True)
    upload = ws / "input.stl"
    _write_parts(upload, sc.parts if sc.upload == "stl-multi" else [])

    # 1. the intake boundary: every change is stated, nothing is dropped
    pre, sub, note = through_intake(sc)
    patches = sub["patches"]
    if len(patches) != len(sc.patches):
        oc.problems.append(f"the boundary dropped or added patches: {sc.patches} -> {patches}")
    for typed, got in zip(sc.patches, patches):
        tn, gn = str(typed["name"]).strip(), got.get("name")
        if tn != gn and f"'{tn}' is now {gn}" not in note:
            oc.problems.append(f"'{tn}' became {gn} without the model being told")
        tt, gt = str(typed.get("type") or "").strip(), got.get("type")
        if _typed_word(tt) != gt and tt != gt and f"typed '{tt}' is a {gt}" not in note:
            oc.problems.append(f"type '{tt}' of {gn} became {gt} without the model being told")
    if at.canonical_payload(pre["selected_engine"], pre["purpose"], pre["input_kind"],
                            pre["dimensionality"], pre["patches"], pre["engine_params"]) != \
            at.canonical_payload(sub["mesh_engine"], sub["purpose"], sub["input_kind"],
                                 sub["dimensionality"], sub["patches"], sub["engine_params"]):
        oc.problems.append("the previewed and the submitted declarations normalise differently - "
                           "the preview token could never authorize the submission")

    # 2. validation + admission, with the facts the intake reads off the upload
    errors = validate_submission(sub)
    verdict = preview_admission(sc.engine, sc.purpose, sc.input_kind, sc.dimensionality,
                                patches=pre["patches"], engine_params=pre["engine_params"],
                                geometry_facts=geometry_facts(sc, upload))
    if errors or verdict["verdict"] != ADMIT_SUPPORTED:
        oc.refusals = list(errors) + ([verdict.get("safe_user_message", "")]
                                      if verdict["verdict"] != ADMIT_SUPPORTED else [])
        return oc
    oc.approved = True
    oc.patches = patches

    # 3. the approved snapshot, the dispatch payload and the run-boundary re-check
    from meshpipeline.pipeline.enums import FIDELITY_POLICY_VERSION, resolve_mesh_fidelity
    req_f, eff_f, src_f = resolve_mesh_fidelity(None)
    fp = at.approved_intent_fingerprint(
        engine=sub["mesh_engine"], purpose=sub["purpose"], input_kind=sub["input_kind"],
        dimensionality=sub["dimensionality"], patches=patches, engine_params=sub["engine_params"],
        request_txt=sub["request_txt"], requested_mesh_fidelity=None,
        effective_mesh_fidelity=eff_f.value, mesh_fidelity_source=src_f.value,
        fidelity_policy_version=FIDELITY_POLICY_VERSION, flow_axis=sub.get("flow_axis"))
    req = SimpleNamespace(
        approved_patch_contract=ApprovedPatchContract.build(patches).to_dict(),
        intake_patches=list(patches), approved_intent_fingerprint=fp,
        mesh_engine=sub["mesh_engine"], purpose=sub["purpose"], input_kind=sub["input_kind"],
        dimensionality=sub["dimensionality"], engine_params=sub["engine_params"],
        requested_mesh_fidelity=None, effective_mesh_fidelity=eff_f.value,
        mesh_fidelity_source=src_f.value, fidelity_policy_version=FIDELITY_POLICY_VERSION,
        request_txt=sub["request_txt"], requested_extents=None, reference_length_m=None,
        flow_axis=sub.get("flow_axis"), requirements_strict=False, geometry_source=None,
        geometry_interpretation=None)
    reason = check_admission(req)
    if reason:
        oc.problems.append(f"the run-boundary re-check refused an approved run: {reason}")
    graph = normalise_submission(sub).intake_patches
    if [(p["name"], p["type"]) for p in graph] != [(p["name"], p["type"]) for p in patches]:
        oc.problems.append(f"the intake graph state carries {graph}, the snapshot {patches}")

    # 4. the builder workspace: the contract the builder and the pre-flight read
    _write_workspace_context_files(
        workspace=ws, source_path="", request_txt=sub["request_txt"],
        review_brief_txt=sub["review_brief_txt"], intake_patches=patches,
        dimensionality=sub["dimensionality"], engine_params=sub["engine_params"],
        flow_topology=topology_of(sc.purpose), purpose=sc.purpose)
    back = [(p["name"], p["type"]) for p in contract_patches(ws)]
    if back != [(p["name"], p["type"]) for p in patches]:
        oc.problems.append(f"the workspace contract reads back {back}, not {patches}")

    # 5. the engine's own case writer and the native run behind the pre-flight
    if sc.engine not in RUNNERS:
        # the multi-region case splits regions after meshing; its written case cannot be read
        # for user boundaries beforehand, and the pre-flight says so rather than guessing
        assert case_contract.read_case_boundary(ws, sc.engine) is None
        return oc
    before = len(world.mesher.runs)
    try:
        RUNNERS[sc.engine](world, sc, ws, patches)
    except AssertionError as exc:
        oc.problems.append(f"the engine path failed: {exc}")
        refusal = case_contract.refusal_of(ws)
        if refusal:
            oc.problems.append(f"pre-flight refusal: {refusal['problems']}")
        return oc
    if len(world.mesher.runs) == before:
        oc.problems.append("no mesher run was launched for an approved case")
    if case_contract.refusal_of(ws):
        oc.problems.append(f"pre-flight refused: {case_contract.refusal_of(ws)['problems']}")
    written = _written_text(ws)
    for p in patches:
        if sc.engine == "vmtk" and p["type"] == "wall":
            continue    # vmtk names no wall in any file: it is entity 1 of the delivered mesh
        if p["name"] not in written:
            oc.problems.append(f"the approved patch {p['name']!r} is written nowhere in the case")
    problems, case = case_contract.check(ws, sc.engine)
    if case is None:
        oc.problems.append("the pre-flight could not read the case this system wrote")
    oc.problems.extend(f"pre-flight: {p}" for p in problems)

    # 6. the manifest and the gates, on a mesh built to the written case
    oc.problems.extend(finalize_and_gate(sc, ws, patches))
    return oc


def _scenarios() -> list[Scenario]:
    rng = random.Random(SEED)
    # external flow on the box-building engines is where a far field, a ground and a body's parts
    # all meet one case writer - the founder's car - so it is drawn three times as often
    combos = [c for c in _combos()
              for _ in range(3 if c[0] in ("snappy", "cfmesh") and c[1] == "external_cfd" else 1)]
    return [generate(rng, combos[i % len(combos)]) for i in range(FUZZ_CASES)]


def _report(outcomes: list[Outcome]) -> None:
    """FUZZ_REPORT=1: the tally, and every broken case in full - for reading a run, not for CI."""
    import collections
    import os
    if not os.environ.get("FUZZ_REPORT"):
        return
    tally: collections.Counter = collections.Counter()
    for o in outcomes:
        tally[(o.sc.engine, o.sc.purpose, "broken" if o.problems else
               "delivered" if o.approved else "refused")] += 1
    for key in sorted(tally):
        print("TALLY", *key, tally[key])
    for o in outcomes:
        if o.problems:
            print("BROKEN", o.sc.label, o.sc.upload, o.sc.parts, o.sc.patches)
            for p in o.problems:
                print("    ", p[:400])
        elif o.sc.must_refuse and o.approved:
            print("LEAKED", o.sc.label, o.sc.must_refuse, o.sc.patches)


def test_what_the_user_approves_is_what_every_engine_delivers(world):
    outcomes = [walk(world, sc, world.root / f"case_{i}" / "attempt_1")
                for i, sc in enumerate(_scenarios())]
    _report(outcomes)

    broken = [f"[{o.sc.label} upload={o.sc.upload} parts={o.sc.parts}] {o.sc.patches}\n    "
              + "\n    ".join(o.problems) for o in outcomes if o.problems]
    assert not broken, (f"{len(broken)} declaration(s) did not reach the mesh as approved:\n"
                        + "\n".join(broken[:15]))

    leaked = [f"[{o.sc.label}] must refuse ({o.sc.must_refuse}): {o.sc.patches}"
              for o in outcomes if o.sc.must_refuse and o.approved]
    assert not leaked, "undeliverable declarations were approved:\n" + "\n".join(leaked[:15])

    silent = [o.sc.label for o in outcomes
              if not o.approved and not any(r.strip() for r in o.refusals)]
    assert not silent, f"refused without saying why: {silent[:10]}"

    # not vacuous: every engine x purpose the registry admits delivered something, and every
    # feature the fixes cover was delivered at least once
    delivered = {(o.sc.engine, o.sc.purpose) for o in outcomes if o.approved}
    for engine, purpose, _ik, _dim in _combos():
        assert (engine, purpose) in delivered, f"no {engine}/{purpose} declaration was approved"
    for feature in ("reserved", "ground", "multi_wall", "symmetry", "meet_after_cleaning"):
        assert any(o.approved and feature in o.sc.features for o in outcomes), \
            f"no approved case exercised {feature}"
    assert any("capitals" in o.sc.features and not o.approved for o in outcomes), \
        "no case repeated a name in other capitals"


def test_the_role_vocabulary_names_every_role_a_purpose_routes_on():
    from meshpipeline.contracts.patch_names import ALL_ROLES
    assert {r for p in PURPOSES.values() for r in p.boundary_roles} == set(ALL_ROLES)


# the mismatches the fuzz found, each pinned by the declaration that exposed it

def _walk_one(world, label: str, sc: Scenario) -> tuple[Outcome, Path]:
    ws = world.root / label / "attempt_1"
    return walk(world, sc, ws), ws


def _boundary(ws: Path) -> dict:
    text = (ws / "constant" / "polyMesh" / "boundary").read_text()
    import re
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"(\w+)\s*\{[^}]*?nFaces\s+(\d+)", text)}


def test_the_car_that_meshed_for_26_minutes_reaches_the_mesh_as_approved(world):
    # job ac1daa3e: "car wall", the ground, the far field - approved, meshed, failed as "quality"
    oc, ws = _walk_one(world, "car", Scenario(
        "founder car", "snappy", "external_cfd", "solid-body", "3D",
        [{"name": "car wall", "type": "wall"}, {"name": "ground", "type": "wall"},
         {"name": "farfield", "type": "farfield"}]))
    assert oc.approved and not oc.problems, oc.problems
    faces = _boundary(ws)
    assert faces["car_wall"] > 0 and faces["ground"] > 0 and faces["farfield"] > 0
    assert "ground { type wall;" in (ws / "system" / "blockMeshDict").read_text()


@pytest.mark.parametrize("farfield", ["far field", "Freestream", "outer boundary", "domain"])
def test_a_far_field_by_any_name_is_the_box_snappy_builds(world, farfield):
    # snappy wrote its box as `farfield` whatever the user called it: zero faces on theirs
    oc, ws = _walk_one(world, "ff", Scenario(
        "far field name", "snappy", "external_cfd", "body-surface", "3D",
        [{"name": "wing", "type": "wall"}, {"name": farfield, "type": "far field"}]))
    assert oc.approved and not oc.problems, oc.problems
    assert _boundary(ws)[oc.patches[1]["name"]] > 0


@pytest.mark.parametrize("ground", ["ground plane", "floor", "Road Surface"])
def test_a_ground_by_any_word_is_the_floor_the_domain_lays(world, ground):
    # "ground plane" became ground_plane, a second BODY wall: the car was staged under one of the
    # two names and the other came back empty - or admission blamed the CAD for naming nothing
    oc, ws = _walk_one(world, "gp", Scenario(
        "ground synonym", "snappy", "external_cfd", "solid-body", "3D",
        [{"name": "car", "type": "wall"}, {"name": ground, "type": "wall"},
         {"name": "farfield", "type": "farfield"}]))
    assert oc.approved and not oc.problems, oc.problems
    assert [p["name"] for p in oc.patches] == ["car", "ground", "farfield"]
    assert _boundary(ws)["ground"] > 0


def test_one_wall_on_a_many_part_stl_is_one_wall_on_cfmesh(world):
    # the parts were written under the file's names; renameBoundary matched none of them and put
    # them all in "fixedWalls" - the approved wall had no faces, and an extra patch appeared
    oc, ws = _walk_one(world, "cfm", Scenario(
        "multi-part single wall", "cfmesh", "external_cfd", "body-surface", "3D",
        [{"name": "Car Body", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
        upload="stl-multi", parts=["Car Body", "wheels", "mirrors"]))
    assert oc.approved and not oc.problems, oc.problems
    assert set(_boundary(ws)) == {"Car_Body", "farfield"}


def test_named_parts_reach_snappy_as_the_declared_walls(world):
    oc, ws = _walk_one(world, "snm", Scenario(
        "multi-part walls", "snappy", "external_cfd", "body-surface", "3D",
        [{"name": "wing left", "type": "wall"}, {"name": "Fuselage", "type": "wall"},
         {"name": "farfield", "type": "farfield"}],
        upload="stl-multi", parts=["Wing Left", "FUSELAGE"]))
    assert oc.approved and not oc.problems, oc.problems
    faces = _boundary(ws)
    assert faces["wing_left"] > 0 and faces["Fuselage"] > 0


def test_several_walls_of_a_2d_section_reach_cfmesh(world):
    # the 2D path wrote the whole section as the first wall; every other approved wall was empty
    oc, ws = _walk_one(world, "c2d", Scenario(
        "multi-element section", "cfmesh", "external_cfd", "body-surface", "2D",
        [{"name": "main", "type": "wall"}, {"name": "flap", "type": "wall"},
         {"name": "farfield", "type": "farfield"}, {"name": "frontAndBack", "type": "empty"}],
        upload="stl-multi", parts=["main", "flap"]))
    assert oc.approved and not oc.problems, oc.problems
    assert {"main", "flap", "frontAndBack"} <= set(_boundary(ws))


@pytest.mark.parametrize("sc", [
    Scenario("two far fields", "snappy", "external_cfd", "body-surface", "3D",
             [{"name": "body", "type": "wall"}, {"name": "inflow", "type": "farfield"},
              {"name": "outflow", "type": "farfield"}]),
    Scenario("two empties", "cfmesh", "external_cfd", "body-surface", "2D",
             [{"name": "af", "type": "wall"}, {"name": "farfield", "type": "farfield"},
              {"name": "front", "type": "empty"}, {"name": "back", "type": "empty"}]),
    Scenario("2D duct", "cfmesh", "internal_cfd", "body-surface", "2D",
             [{"name": "wall", "type": "wall"}, {"name": "in", "type": "inlet", "diameter_mm": 20},
              {"name": "out", "type": "outlet", "diameter_mm": 80},
              {"name": "sides", "type": "empty"}], upload="step"),
    Scenario("two walls in a duct", "snappy", "internal_cfd", "solid-body", "3D",
             [{"name": "pipe", "type": "wall"}, {"name": "flange", "type": "wall"},
              {"name": "in", "type": "inlet", "diameter_mm": 20},
              {"name": "out", "type": "outlet", "diameter_mm": 80}], upload="step"),
    Scenario("two walls in a lumen", "vmtk", "internal_cfd", "body-surface", "3D",
             [{"name": "lumen", "type": "wall"}, {"name": "stent", "type": "wall"},
              {"name": "in", "type": "inlet", "diameter_mm": 20},
              {"name": "out", "type": "outlet", "diameter_mm": 80}], upload="lumen",
             engine_params={"wall_layers": "on"}),
    Scenario("symmetry in a duct", "snappy", "internal_cfd", "solid-body", "3D",
             [{"name": "pipe", "type": "wall"}, {"name": "mid", "type": "symmetry"},
              {"name": "in", "type": "inlet", "diameter_mm": 20},
              {"name": "out", "type": "outlet", "diameter_mm": 80}], upload="step"),
    Scenario("STEP parts on a flattening engine", "cfmesh", "external_cfd", "solid-body", "3D",
             [{"name": "wing", "type": "wall"}, {"name": "fuselage", "type": "wall"},
              {"name": "farfield", "type": "farfield"}], upload="step-assembly",
             parts=["wing", "fuselage"]),
], ids=lambda s: s.label)
def test_what_no_case_writer_can_build_is_refused_at_intake_with_the_way_on(world, sc):
    oc, _ws = _walk_one(world, "refuse", sc)
    assert not oc.approved, f"{sc.label} was approved and would have failed after the run"
    said = " ".join(oc.refusals)
    assert said.strip() and ("Declare" in said or "declare" in said or "Drop" in said
                             or "drop" in said or "revise" in said), said


def test_a_case_that_drops_an_approved_name_stops_before_the_mesher(world, monkeypatch):
    # the old far-field renderer, put back: the pre-flight must stop it in the first pass, with no
    # re-plan and no mesher run, and the executor must end the job as an internal error naming it
    import meshpipeline.engines.snappy.drivers as drv
    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.errors import FailureClass, SystemFailure

    monkeypatch.setattr("meshpipeline.engines.declared_boundary.farfield_name",
                        lambda *_a, **_k: "farfield")
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 3, raising=False)
    plans = []
    real_plan = planner.plan_with_accounting

    async def _counting(**kw):
        plans.append(kw)
        return await real_plan(**kw)
    monkeypatch.setattr(planner, "plan_with_accounting", _counting)
    oc, ws = _walk_one(world, "sabotage", Scenario(
        "old far field", "snappy", "external_cfd", "body-surface", "3D",
        [{"name": "wing", "type": "wall"}, {"name": "freestream", "type": "farfield"}]))
    assert oc.approved
    assert world.mesher.runs == [], "a mesher ran on a case the pre-flight refused"
    assert len(plans) == 1, "the driver re-planned a case whose names cannot change"
    refusal = case_contract.refusal_of(ws)
    assert refusal and any("freestream" in p for p in refusal["problems"])

    import meshpipeline.pipeline.executor as ex
    from meshpipeline.application import execution_fence

    async def _owner(*_a, **_k):
        return None
    monkeypatch.setattr(ex, "execution_publisher", lambda *a, **k: _Publish())
    monkeypatch.setattr(execution_fence, "assert_current_owner", _owner)
    with pytest.raises(SystemFailure) as exc:
        asyncio.run(ex.node_executor({"job_id": "j", "openfoam_workspace": str(ws),
                                      "engine": "snappy"}))
    assert exc.value.failure_class is FailureClass.INTERNAL
    assert "freestream" in exc.value.operator_detail
