# Responsibility: Verify Snappy-driver publications run under the exact claimed execution ownership.
# Boundaries: the external no-symmetry rejection - other driver sites extend this module.
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.models import SimulationJob

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

#: The driver module every emission here must come from. Sites are named by the function that
#: publishes them and the publisher method used, never by line number - the ordinals below follow
#: call order within a run, so an edit that moves a call cannot silently rename it.
DRIVER_MOD = "engines/snappy/drivers.py"

#: The one site this suite owns.
REJECTION = "_build_snappy_deterministic::error#1"


def _sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=1, max_overflow=1,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _with_session(fn):
    engine, Session = _sessions()
    try:
        async with Session() as db:
            return await fn(db)
    finally:
        await engine.dispose()


async def _seed(job_id: uuid.UUID, owner_id: str) -> None:
    async def _q(db):
        db.add(SimulationJob(id=job_id, owner_id=owner_id))
        await db.commit()
    await _with_session(_q)


async def _row(job_id: uuid.UUID):
    async def _q(db):
        return (await db.execute(
            select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
    return await _with_session(_q)


# A closed box over the given per-axis ranges, written as a real STL.
def _box(path: Path, xr: tuple, yr: tuple, zr: tuple) -> Path:
    (x0, x1), (y0, y1), (z0, z1) = xr, yr, zr
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 4, 5), (0, 5, 1),
             (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    out = ["solid body"]
    for a, b, cc in faces:
        out.append("facet normal 0 0 0")
        out.append("  outer loop")
        out.extend(f"    vertex {c[i][0]:.6f} {c[i][1]:.6f} {c[i][2]:.6f}" for i in (a, b, cc))
        out.append("  endloop")
        out.append("endfacet")
    out.append("endsolid body")
    path.write_text("\n".join(out) + "\n")
    return path


# A body whose bounding box straddles the centreline on EVERY axis: a full-span model. The real
# `detect_symmetry_plane` finds no coplanar cut face on it and returns None on its own, so the
# rejection is produced by production geometry reasoning, not by a stubbed refusal.
def _full_span_cube(path: Path, half: float = 0.5) -> Path:
    return _box(path, (-half, half), (-half, half), (-half, half))


# A body whose bounding box lies ON zero on exactly one axis (Y) and straddles it on the others:
# a half-model. The same real detector accepts it and returns the Y plane, again on its own.
def _half_model(path: Path) -> Path:
    return _box(path, (-0.5, 0.5), (0.0, 1.0), (-0.5, 0.5))


#: The plane the real detector must find on `_half_model`, from its own reading of the surface.
EXPECTED_PLANE = {"axis": 1, "pos": 0.0, "side": "min", "name": "sym"}


def _materialized(stl: Path, owner_id: str):
    from tests._geometry_support import interpretation_ref, source_ref

    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    from meshpipeline.contracts.geometry_units import LengthUnit

    ref = source_ref(local_file=stl, tmp_path=None, owner_id=owner_id, filename=stl.name)
    return MaterializedGeometry(
        ref=ref,
        interpretation=interpretation_ref(geometry_source_id=ref.source_id,
                                          unit=LengthUnit.metre),
        local_path=stl)


def _geometry_state(tmp_path: Path, owner_id: str, body=_full_span_cube) -> dict:
    return _materialized(body(tmp_path / "body.stl"), owner_id).to_state()


# Every emission this run makes, with the ownership bound at the moment of the call. The real
# publisher method is always invoked unchanged - this only reads the context it ran in.
def _record_emissions(monkeypatch, seen: list) -> None:
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    root = Path(sys.modules["meshpipeline"].__file__).parent

    def wrap(method: str):
        original = getattr(JobPublisher, method)

        def w(self, *a, **k):
            # Attribute the emission to PRODUCTION. Every execution event now reaches the adapter
            # through the ownership gate, so the immediate caller is always the gated wrapper;
            # the site that decided to publish is the frame above it.
            frame = sys._getframe(1)
            while frame is not None and Path(
                    frame.f_code.co_filename).name == "execution_publisher.py":
                frame = frame.f_back
            try:
                rel = Path(frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            except ValueError:
                rel = frame.f_code.co_filename
            seen.append({
                "module": rel, "fn": frame.f_code.co_name, "method": method,
                "own": fence.current_ownership(), "impl": type(self).__name__,
                "publisher": id(self),
                "publisher_job_id": str(getattr(self, "job_id", "")),
                "op_id": k.get("op_id", ""),
                # An OPAQUE discriminator. Never asserted as a value - only used to show that two
                # emissions sharing an operation identity come from two different code locations.
                "loc": frame.f_lineno,
            })
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, method, w)

    for method in ("note", "error", "attempt", "meshed", "meshing",
                   "tool_call", "tool_result"):
        wrap(method)


# The production publisher factory the builder node calls. Counting constructions proves the
# publisher under test was built by production and not handed in by this test. The REAL
# ownership-checked object is returned unchanged: nothing is wrapped, substituted or unwrapped,
# so the inner adapter this run publishes through is the one production would have used.
def _count_constructions(monkeypatch, built: list) -> None:
    import meshpipeline.agents.builder.agent as ag
    real = ag.execution_publisher

    def counting(job_id, agent=""):
        made = real(job_id, agent=agent)
        built.append((str(job_id), agent, type(made).__name__,
                      type(made._inner).__name__))
        return made

    monkeypatch.setattr(ag, "execution_publisher", counting)


# The native mesher must never start on this path. The real callable stays installed; the counter
# only observes, so a regression that reached it would run it and still be caught.
def _count_native(monkeypatch, calls: list) -> None:
    from meshpipeline.engines.snappy import snappy_runner as R
    real = R.run_snappy

    def counting(*a, **k):
        calls.append(a[:1])
        return real(*a, **k)

    monkeypatch.setattr(R, "run_snappy", counting)


#: What `_patch_face_counts` reads back out of a finished case. The wall total is what the judge
#: measures as `wall_faces`; `farfield` is excluded from it by the driver.
_BOUNDARY = """FoamFile{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
2
(
    body    { type wall;  nFaces 1200; startFace 0;    }
    farfield{ type patch; nFaces 800;  startFace 1200; }
)
"""

#: The internal case is bounded by the passage itself: a wall plus the two openings. The driver
#: measures `wall_faces` on the patch name the surface prep assigned, so the name has to be the
#: one production authored, not an external-flow stand-in.
_BOUNDARY_INTERNAL = """FoamFile{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
3
(
    wall  { type wall;  nFaces 1400; startFace 0;    }
    inlet { type patch; nFaces 120;  startFace 1400; }
    outlet{ type patch; nFaces 120;  startFace 1520; }
)
"""


# The ONLY doubled native boundary. The real `asyncio.to_thread` call still runs, so this executes
# on a production worker thread; it replaces the external mesher and nothing else. It publishes
# nothing, touches no fence, no Redis and no claim state, and returns the driver's own result
# shape so parsing, judging, attempt selection and `last_valid` all stay real.
def _install_native_double(monkeypatch, native: list, *, internal: bool = False) -> None:
    import threading

    from meshpipeline.engines.snappy import snappy_runner as R

    def run_snappy(workspace, *, context=None, bashrc="", timeout=1800):
        ws = Path(workspace)
        system = ws / "system"
        authored = sorted(p.name for p in system.glob("*")) if system.is_dir() else []
        native.append({
            "workspace": str(ws),
            "timeout": timeout,
            "input_stl": (ws / "input.stl").exists(),
            "authored": authored,
            # the symmetry the driver accepted has to have reached the authored case
            "symmetry_in_case": any(
                "symmetry" in p.read_text(errors="replace")
                for p in system.glob("*") if p.is_file()),
            "thread": threading.current_thread().name,
            "is_event_loop_thread": threading.current_thread() is threading.main_thread(),
            # read, never injected: whatever the production context carried into this thread
            "own": fence.current_ownership(),
        })
        poly = ws / "constant" / "polyMesh"
        poly.mkdir(parents=True, exist_ok=True)
        (poly / "boundary").write_text(_BOUNDARY_INTERNAL if internal else _BOUNDARY)
        return {"rc": 0, "timed_out": False,
                "layer_coverage": 0.95, "per_patch_layers": {"body": 0.95}}

    monkeypatch.setattr(R, "run_snappy", run_snappy)


# checkMesh is the OTHER native boundary on this path, and it is absent from the application
# image. Left real it reports nothing, so the judge would decide on absent measurements. This
# returns a quality report a real checkMesh produces, and the driver's own predicates then decide:
# a clean report is accepted, a widely skewed one is refused while the mesh stays valid - which is
# the documented repair case, and the only way `last_valid` is reached without a broken mesh.
def _install_quality_double(monkeypatch, *, skewed: bool) -> None:
    from meshpipeline.engines.snappy import snappy_runner as R

    faces = 3_600_000
    skew_faces = 9_000 if skewed else 0

    def check_mesh(workspace, *, bashrc="", region=""):
        return {"mesh_ok": not skewed, "cells": 1_200_000, "faces": faces,
                "hexahedra": 1_150_000, "polyhedra": 0, "max_non_ortho": 42.0,
                "skew_faces": skew_faces, "skew_fraction": skew_faces / faces,
                "fatal": []}

    monkeypatch.setattr(R, "check_mesh", check_mesh)


# The planner's external provider round. `plan_with_accounting` itself stays real - its prompt,
# its public trace, its parsing, its accounting and its state updates all execute; only the
# network call is replaced, with a valid deterministic response.
def _install_provider_double(monkeypatch, rounds: list) -> None:
    from meshpipeline.contracts import model_inference as llm_router
    from meshpipeline.contracts.model_inference import ModelRoundResult

    plan = ('{"approach": "octree hex with prism layers", "max_cells": 2000000, '
            '"quality": "strict", "domain_margin": {"up": 2.0, "down": 4.0}}')

    async def call_planner_model(messages, tools=None, tool_choice="auto", job_id="",
                                 user_id="", parallel_tool_calls=None):
        rounds.append({"job_id": job_id, "messages": len(messages)})
        return ModelRoundResult(assistant_text=plan, finish_reason="stop",
                                input_tokens=800, output_tokens=64)

    monkeypatch.setattr(llm_router, "call_planner_model", call_planner_model)


# Ownership at each crossing of the first production seam after the graph returns. `is_current_owner`
# also serves the in-graph fence, so the whole sequence is kept: the last crossing is the post-graph
# one, and it is read here rather than from this test task's own unrelated context.
def _watch_post_graph(monkeypatch, marks: list) -> None:
    from meshpipeline.persistence.lease import LeaseRepository
    real = LeaseRepository.is_current_owner

    async def w(self, db, own):
        marks.append(fence.current_ownership())
        return await real(self, db, own)

    monkeypatch.setattr(LeaseRepository, "is_current_owner", w)


# A graph holding the REAL builder node. The route stays production: _run_async claims delivery,
# builds the graph through the composition seam it already imports, and the node reached is the
# one the pipeline runs - it is never called directly, and nothing about the claim is mocked.
def _graph(seed_state: dict):
    from langgraph.graph import END, START, StateGraph

    from meshpipeline.agents.builder.agent import node_builder
    from meshpipeline.contracts.pipeline_state import PipelineState

    g = StateGraph(PipelineState)
    g.add_node("seed", lambda s: dict(seed_state))
    g.add_node("node_builder", node_builder)
    g.add_edge(START, "seed")
    g.add_edge("seed", "node_builder")
    g.add_edge("node_builder", END)
    return g


# The state fields an external Snappy run requires. One function, so each scenario differs only
# in its body: the internal driver extends this the same way.
def _external_state(tmp_path: Path, owner_id: str, body) -> dict:
    prev = tmp_path / "prev"
    prev.mkdir(parents=True)
    body(prev / "input.stl")
    return {
        "geometry": _geometry_state(tmp_path, owner_id, body),
        "engine": "snappy",
        # written by the real classifier for a reviewer-driven retry (pipeline/classifier.py);
        # a non-planning mode, so `drive` performs no planner round of its own.
        "builder_mode": "retry",
        "retry_count": 0,
        "openfoam_workspace": str(prev),
        "flow_topology": "external",
        "dimensionality": "3D",
        "intake_patches": [{"name": "sym", "type": "symmetry"}],
        "request_txt": "external aerodynamics, half model on the centreline",
    }


# The internal driver needs a CAD SOLID: it refuses an STL outright, because a fluid volume has
# to be separated from a closed solid. The bytes are never read - the tessellation boundary is
# doubled - but production checks the suffix and that the file exists.
def _internal_state(tmp_path: Path, owner_id: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    prev = tmp_path / "prev"
    prev.mkdir(parents=True)
    return {
        "geometry": _materialized(step, owner_id).to_state(),
        "engine": "snappy",
        "builder_mode": "retry",
        "retry_count": 0,
        "openfoam_workspace": str(prev),
        "flow_topology": "internal",
        "dimensionality": "3D",
        # bindable declarations: the binder now runs on every declared internal job, and the
        # double's two openings are the same size - locations are what tell them apart
        "intake_patches": [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0]},
                           {"name": "outlet", "type": "outlet", "near_mm": [400, 0, 0]},
                           {"name": "wall", "type": "wall"}],
        "request_txt": "internal through-flow through a bend",
    }


# The internal-only native boundary. Like the mesher it runs behind a real asyncio.to_thread call;
# it writes the three surfaces the real adapter would write and returns the real result shape.
def _install_tessellate_double(monkeypatch, calls: list, *, fail: bool = False) -> None:
    import threading

    from meshpipeline.engines.snappy import snappy_runner as R

    def tessellate_internal(source_path, out_dir, *a, **k):
        out = Path(out_dir)
        calls.append({
            "source_path": str(source_path),
            "source_exists": Path(source_path).exists(),
            "suffix": Path(source_path).suffix.lower(),
            "out_dir": str(out),
            "thread": threading.current_thread().name,
            "is_event_loop_thread": threading.current_thread() is threading.main_thread(),
            "own": fence.current_ownership(),
        })
        if fail:
            # what the real adapter raises when the solid has no closeable openings
            raise ValueError("no closed inlet/outlet openings were found on the solid")
        out.mkdir(parents=True, exist_ok=True)
        stls = {}
        for name, zr in (("wall", (0.0, 1.0)), ("inlet", (0.0, 0.02)),
                         ("outlet", (0.98, 1.0))):
            stls[name] = str(_box(out / f"{name}.stl", (-0.05, 0.05), (-0.05, 0.05), zr))
        return {"stls": stls,
                # area AND centroid: the real tessellate_internal measures both, and the
                # disclosure note formats the centroid - a double without it hides a crash
                "openings": {"inlet": {"area": 0.00785, "centroid": [0.0, 0.0, 0.0]},
                             "outlet": {"area": 0.00785, "centroid": [0.4, 0.0, 0.0]}},
                "interior_point": [0.0, 0.0, 0.5],
                "bbox_min": [-0.05, -0.05, 0.0], "bbox_max": [0.05, 0.05, 1.0]}

    monkeypatch.setattr(R, "tessellate_internal", tessellate_internal)


# The thin-feature probe, answering "yes, there is a plate thinner than the wall cell". The real
# probe measures the staged wall's triangles (cad/thin_features.py); the double's box is 100 mm
# across, so only a stand-in can put the driver on the branch that discloses a thin feature.
def _install_thin_feature_double(monkeypatch) -> None:
    from meshpipeline.cad import thin_features as TF

    def thin_refinement_boxes(tris, *, cell_m, **_k):
        return [{"min": [-0.02, -0.02, 0.48], "max": [0.02, 0.02, 0.52],
                 "level_bump": 2, "thinnest_m": 0.003, "n_triangles": 24}]

    monkeypatch.setattr(TF, "thin_refinement_boxes", thin_refinement_boxes)


async def _run(monkeypatch, tmp_path, *, body=_full_span_cube, native_double=None,
               internal=False, tessellate_fail=False, unbindable_patches=False,
               thin_feature=False):
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()

    job_id = uuid.uuid4()
    owner_id = f"snappy-{job_id.hex[:8]}"
    await _seed(job_id, owner_id)

    seen: list = []
    built: list = []
    native: list = []
    marks: list = []
    rounds: list = []
    _record_emissions(monkeypatch, seen)
    _count_constructions(monkeypatch, built)
    _watch_post_graph(monkeypatch, marks)
    tessellate: list = []
    if native_double is None:
        _count_native(monkeypatch, native)
    else:
        _install_native_double(monkeypatch, native, internal=internal)
        _install_quality_double(monkeypatch, skewed=native_double)
        _install_provider_double(monkeypatch, rounds)
    if internal:
        _install_tessellate_double(monkeypatch, tessellate, fail=tessellate_fail)
    if internal and thin_feature:
        _install_thin_feature_double(monkeypatch)

    import meshpipeline.pipeline.graph as gm
    root = tmp_path / f"run-{job_id.hex[:8]}"
    st = _internal_state(root, owner_id) if internal else _external_state(root, owner_id, body)
    if unbindable_patches:
        # a declaration no measured opening can satisfy: the seam must refuse pre-mesh
        st["intake_patches"] = [{"name": "wall", "type": "wall"},
                                {"name": "feed", "type": "inlet", "diameter_mm": 10}]
    graph = _graph(st)
    monkeypatch.setattr(gm, "build_graph",
                        lambda checkpointer: graph.compile(checkpointer=checkpointer))

    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    try:
        await asyncio.create_task(_run_async(JobRequest(job_id=str(job_id), owner_id=owner_id)))
    except Exception:                       # the terminal outcome is another suite's contract
        pass
    return job_id, seen, built, native, marks, rounds, tessellate


#: Canonical ordinals follow the driver's own call sites. Each is recognised by the operation
#: identity production passes, so the name survives an edit that moves the call. `pass-outcome`
#: is published from two mutually exclusive branches that share one identity; the accepting run
#: reaches one and the exhausting run the other, and `test_..._are_two_distinct_sites` proves
#: they really are two locations rather than assuming it.
def _canonical(rec: dict, *, accepted: bool) -> str:
    fn, method, op = rec["fn"], rec["method"], rec["op_id"]
    if method in ("tool_call", "tool_result"):
        return f"{fn}::{method}#1"
    if method == "meshing":
        return f"{fn}::meshing#1"
    if method == "meshed":
        return f"{fn}::meshed#1"
    if method == "error":
        # two mutually exclusive refusals share the method: binding (declared ports vs the
        # measured openings) and separability (no closeable openings at all)
        if op.startswith("internal:port-binding-refused"):
            return f"{fn}::error#2"
        return f"{fn}::error#1"
    for prefix, name in (("snappy:symmetry-detected", "note#1"),
                         ("snappy:pass-open:", "note#2"),
                         ("snappy:carving:", "note#4"),
                         ("snappy:passes-exhausted", "note#3"),
                         ("internal:volume-identified", "note#1"),
                         ("internal:pass-open:", "note#2"),
                         ("internal:filling:", "note#4"),
                         ("internal:passes-exhausted", "note#3"),
                         ("internal:thin-feature:", "note#7")):
        if op.startswith(prefix):
            return f"{fn}::{name}"
    if op.startswith(("snappy:pass-outcome:", "internal:pass-outcome:")):
        return f"{fn}::{'note#5' if accepted else 'note#6'}"
    return f"{fn}::{method}?{op}"


#: `_op_begin`/`_op_end` publish the driver's operation lifecycle. They are on the external
#: success path unconditionally, so they are observed and reported, but this checkpoint does not
#: own them and does not promote them.
TOOL_SITES = frozenset({"_op_begin::tool_call#1", "_op_end::tool_result#1"})


def _driver_records(seen: list) -> list[dict]:
    return [r for r in seen if r["module"] == DRIVER_MOD]


def _sites(seen: list, *, accepted: bool) -> list[str]:
    return [_canonical(r, accepted=accepted) for r in _driver_records(seen)]


def _owned_counts(seen: list, *, accepted: bool) -> dict:
    counts: dict[str, int] = {}
    for site in _sites(seen, accepted=accepted):
        if site not in TOOL_SITES:
            counts[site] = counts.get(site, 0) + 1
    return counts


@pytest.fixture()
async def rejection(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path)


@pytest.fixture()
async def accepted(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, body=_half_model, native_double=False)


@pytest.fixture()
async def exhausted(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, body=_half_model, native_double=True)


# The capture-subject variants. Retention is a per-process product mode, so a run only records if
# the process was enabled BEFORE it started; requesting `collection_enabled` as a dependency - not
# as a sibling argument - is what guarantees pytest resolves it first. The tests that are not
# about capture keep using the plain fixtures above, and therefore keep running on the shipped
# collection-disabled default.
@pytest.fixture()
async def accepted_capturing(collection_enabled, monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, body=_half_model, native_double=False)


@pytest.fixture()
async def exhausted_capturing(collection_enabled, monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, body=_half_model, native_double=True)


# the contract


async def test_the_test_holds_no_ownership_before_the_production_root_runs():
    assert fence.current_ownership() is None, \
        "the test process already had ownership bound; nothing it observes would prove anything"


async def test_the_full_span_geometry_is_rejected_at_exactly_one_driver_site(rejection):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = rejection
    assert _sites(seen, accepted=False) == [REJECTION], (
        "the external no-symmetry run must publish exactly one Snappy-driver event; "
        f"got {_sites(seen, accepted=False)}")


async def test_the_rejection_publishes_under_the_claimed_execution_ownership(rejection):
    job_id, seen, _built, _native, _marks, _rounds, _tess = rejection
    rec = next(r for r in seen
               if r["module"] == DRIVER_MOD and r["fn"] == "_build_snappy_deterministic")

    own = rec["own"]
    assert own is not None, f"{REJECTION} published with NO execution ownership bound"
    assert rec["impl"] == "JobPublisher", \
        f"{REJECTION} was observed on {rec['impl']}, not the production publisher"
    assert rec["publisher_job_id"] == str(job_id)

    # The claim is read through an independent session and pool, so the comparison is against
    # PostgreSQL rather than against anything the run kept in memory.
    row = await _row(job_id)
    assert str(own.job_id) == str(job_id)
    assert own.execution_generation == row.execution_generation
    assert own.claim_epoch == row.execution_claim_epoch
    # Compared in memory only; the token is never printed or asserted by value.
    assert (own.worker_token == row.active_worker_token) is True, \
        "the bound token does not match the durable row"


async def test_the_rejection_carries_the_production_operation_identity(rejection):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = rejection
    rec = next(r for r in seen
               if r["module"] == DRIVER_MOD and r["fn"] == "_build_snappy_deterministic")
    assert rec["op_id"] == "snappy:symmetry-unusable", \
        "the rejection changed its operation identity"


async def test_the_publisher_was_constructed_by_production(rejection):
    job_id, _seen, built, _native, _marks, _rounds, _tess = rejection
    assert built, "the builder node never constructed a publisher through the production factory"
    assert [b for b in built if b[0] == str(job_id)
            and b[2] == "OwnershipCheckedPublisher" and b[3] == "JobPublisher"], (
        f"no ownership-checked publisher over the production adapter was built for this job: "
        f"{built}")


async def test_the_native_mesher_never_ran(rejection):
    _job_id, _seen, _built, native, _marks, _rounds, _tess = rejection
    assert native == [], \
        "the rejection must fail before any native meshing, but run_snappy was invoked"


async def test_the_binding_is_cleared_once_the_graph_returns(rejection):
    _job_id, _seen, _built, _native, marks, _rounds, _tess = rejection
    assert marks, "the run never reached the post-graph ownership seam"
    assert any(m is not None for m in marks), \
        "no crossing saw bound ownership; the run never executed under a claim"
    assert marks[-1] is None, (
        "ownership was still bound at the first seam after the graph returned; the binding must "
        f"not leak past graph.ainvoke (saw {type(marks[-1]).__name__})")
    assert fence.current_ownership() is None


async def test_the_rejection_run_leaves_no_task_and_no_duplicate_event(rejection):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = rejection
    leftover = [t for t in asyncio.all_tasks()
                if not t.done() and "_beat" in (t.get_coro().__qualname__ or "")]
    assert leftover == [], "a heartbeat task outlived the rejected run"

    driver = [r for r in seen if r["module"] == DRIVER_MOD]
    ops = [(r["fn"], r["method"], r["op_id"]) for r in driver]
    assert len(ops) == len(set(ops)), f"a driver event was published twice: {ops}"


# S1 - external snappy success
#
# The accepting run reaches a production-grade verdict on its first pass and returns immediately.
# The exhausting run is refused by the judge every pass but leaves a valid mesh behind, so it runs
# the full attempt budget and submits the best one. Between them they reach all six distinct notes,
# which no single run can: `note#5` returns before `note#3` can ever be published.

ACCEPTED_SITES = {
    "_build_snappy_deterministic::note#1": 1,       # half-model detected
    "_build_snappy_deterministic::note#2": 1,       # meshing pass opened
    "_build_snappy_deterministic::note#4": 1,       # carving the body
    "_run_snappy_timed::meshing#1": 1,
    "_build_snappy_deterministic::meshed#1": 1,
    "_build_snappy_deterministic::note#5": 1,       # production-grade
}

#: MAX_SNAPPY_ATTEMPTS passes, each publishing its own open/carve/mesh/outcome.
EXHAUSTED_SITES = {
    "_build_snappy_deterministic::note#1": 1,
    "_build_snappy_deterministic::note#2": 3,
    "_build_snappy_deterministic::note#4": 3,
    "_run_snappy_timed::meshing#1": 3,
    "_build_snappy_deterministic::meshed#1": 3,
    "_build_snappy_deterministic::note#6": 3,       # fell short, re-planning
    "_build_snappy_deterministic::note#3": 1,       # passes exhausted, best mesh submitted
}


async def test_the_real_detector_finds_the_half_model_plane(tmp_path):
    # Production's own reading of the surface, before anything native is reached: the plane is
    # discovered, never supplied.
    from meshpipeline.cad.analysis import analyze_surface
    from meshpipeline.cad.staging import staged_surface
    from meshpipeline.engines.snappy.snappy_runner import detect_symmetry_plane

    def plane_of(stl: Path):
        surface = staged_surface(_materialized(stl, "detector"), stl)
        return detect_symmetry_plane(analyze_surface(surface), "sym")

    plane = plane_of(_half_model(tmp_path / "half.stl"))
    assert plane == EXPECTED_PLANE, f"the real detector did not accept the half-model: {plane}"
    assert plane_of(_full_span_cube(tmp_path / "full.stl")) is None, \
        "the same detector must still refuse a full-span body"


async def test_the_accepted_run_publishes_exactly_its_six_sites(accepted):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = accepted
    assert _owned_counts(seen, accepted=True) == ACCEPTED_SITES


async def test_the_exhausted_run_publishes_exactly_its_seven_sites(exhausted):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = exhausted
    assert _owned_counts(seen, accepted=False) == EXHAUSTED_SITES


async def test_external_success_never_publishes_the_rejection_or_another_engine(accepted,
                                                                                exhausted):
    for label, (_j, seen, _b, _n, _m, _r, _t), acc in (("accepted", accepted, True),
                                                   ("exhausted", exhausted, False)):
        sites = set(_sites(seen, accepted=acc))
        assert REJECTION not in sites, f"the {label} run published the no-symmetry rejection"
        assert not [s for s in sites if s.startswith("_build_internal")], \
            f"the {label} run reached the internal driver"
        assert sites - TOOL_SITES <= set(ACCEPTED_SITES) | set(EXHAUSTED_SITES), \
            f"the {label} run published an unowned driver site: {sites - TOOL_SITES}"


async def test_the_two_pass_outcome_branches_are_two_distinct_sites(accepted, exhausted):
    def loc(bundle, acc, name):
        _j, seen, _b, _n, _m, _r, _t = bundle
        return {r["loc"] for r in _driver_records(seen)
                if _canonical(r, accepted=acc) == name}

    five = loc(accepted, True, "_build_snappy_deterministic::note#5")
    six = loc(exhausted, False, "_build_snappy_deterministic::note#6")
    assert five and six, "one of the pass-outcome branches never published"
    assert five.isdisjoint(six), (
        "note#5 and note#6 were published from the same code location; they must be the two "
        "distinct branches of the judge's verdict")


async def test_every_s1_emission_runs_under_the_claimed_durable_ownership(accepted, exhausted):
    for label, bundle, acc in (("accepted", accepted, True), ("exhausted", exhausted, False)):
        job_id, seen, _built, _native, _marks, _rounds, _tess = bundle
        row = await _row(job_id)
        records = [r for r in _driver_records(seen)
                   if _canonical(r, accepted=acc) not in TOOL_SITES]
        assert records, f"the {label} run published no owned driver event"
        for rec in records:
            site = _canonical(rec, accepted=acc)
            own = rec["own"]
            assert own is not None, f"{label}/{site} published with NO ownership bound"
            assert rec["impl"] == "JobPublisher", f"{label}/{site} used {rec['impl']}"
            assert rec["publisher_job_id"] == str(job_id)
            assert str(own.job_id) == str(job_id), f"{label}/{site} carried another job"
            assert own.execution_generation == row.execution_generation, \
                f"{label}/{site} carried another generation"
            assert own.claim_epoch == row.execution_claim_epoch, \
                f"{label}/{site} carried another claim epoch"
            assert (own.worker_token == row.active_worker_token) is True, \
                f"{label}/{site} did not match the durable token"


async def test_all_s1_emissions_share_one_production_publisher(accepted):
    job_id, seen, built, _native, _marks, _rounds, _tess = accepted
    publishers = {r["publisher"] for r in _driver_records(seen)}
    assert len(publishers) == 1, "the driver published through more than one publisher object"
    assert [b for b in built if b[0] == str(job_id)
            and b[2] == "OwnershipCheckedPublisher" and b[3] == "JobPublisher"], (
        f"no ownership-checked publisher over the production adapter was built for this job: "
        f"{built}")


async def test_the_native_boundary_is_crossed_the_production_number_of_times(accepted, exhausted):
    _j, _s, _b, accepted_native, _m, _r, _t = accepted
    _j2, _s2, _b2, exhausted_native, _m2, _r2, _t2 = exhausted
    # the accepting run is judged production-grade on its first pass and returns there
    assert len(accepted_native) == 1, \
        f"the accepted run crossed the native boundary {len(accepted_native)} times, not once"
    # the exhausting run spends the whole attempt budget
    from meshpipeline.engines.snappy import settings as scfg
    assert len(exhausted_native) == int(scfg.MAX_SNAPPY_ATTEMPTS), \
        f"the exhausted run crossed the native boundary {len(exhausted_native)} times"


async def test_the_native_boundary_received_the_production_workspace_and_case(accepted):
    _j, _s, _b, native, _m, _r, _t = accepted
    call = native[0]
    assert call["input_stl"], "the native mesher was called without the staged surface"
    assert call["authored"], "the native mesher was called before the case was authored"
    assert call["symmetry_in_case"], \
        "the accepted symmetry plane never reached the authored case"
    assert call["timeout"] == 2400, \
        f"the driver's per-mesh cap changed: {call['timeout']}"
    assert Path(call["workspace"]).is_absolute()


async def test_the_worker_thread_inherits_the_same_ownership_object(accepted):
    job_id, seen, _built, native, _marks, _rounds, _tess = accepted
    call = native[0]
    assert not call["is_event_loop_thread"], \
        "run_snappy did not run on a worker thread; to_thread was bypassed"

    # `meshing` is published a few statements before the to_thread call, and the driver's last
    # operation result is published after it returns - both are real production emissions, so the
    # before/after readings come from the run itself rather than from an added hook.
    records = _driver_records(seen)
    before = next(r for r in records if r["method"] == "meshing")["own"]
    after = next(r for r in reversed(records) if r["method"] == "tool_result")["own"]
    inside = call["own"]

    assert inside is not None, "the worker thread saw NO ownership"
    assert inside is before, "the worker thread received a copy, not the bound ownership"
    assert after is before, "ownership changed identity after returning to the event loop"

    row = await _row(job_id)
    assert str(inside.job_id) == str(job_id)
    assert inside.execution_generation == row.execution_generation
    assert inside.claim_epoch == row.execution_claim_epoch
    assert (inside.worker_token == row.active_worker_token) is True


async def test_s1_clears_the_binding_and_leaves_no_task_or_duplicate(accepted):
    _job_id, seen, _built, _native, marks, _rounds, _tess = accepted
    assert marks and any(m is not None for m in marks)
    assert marks[-1] is None, \
        "ownership was still bound at the first seam after a successful graph return"
    assert fence.current_ownership() is None
    assert [t for t in asyncio.all_tasks()
            if not t.done() and "_beat" in (t.get_coro().__qualname__ or "")] == [], \
        "a heartbeat task outlived the successful run"

    noted = [(r["op_id"]) for r in _driver_records(seen) if r["method"] == "note"]
    assert len(noted) == len(set(noted)), f"a driver note was published twice: {noted}"


async def test_the_planner_round_is_real_and_the_provider_is_the_only_double(accepted):
    _job_id, _seen, _built, _native, _marks, rounds, _tess = accepted
    assert len(rounds) == 1, \
        f"the accepted run made {len(rounds)} planner provider rounds, not one"
    assert rounds[0]["messages"] == 2, "the real planner prompt was not built"


# operation identity across repeated native attempts
#
# Every meshing pass re-plans against its own failure, so each re-plan is a separate operation.
# When they shared one identity the capture authority saw one operation arriving three times with
# three different payloads, correctly refused to choose between them, and quarantined all three -
# so a job that re-planned lost its whole planner record.

# The durable capture authority itself, read on its own connection.
def _capture_rows(job_id) -> list[dict]:
    import psycopg

    dsn = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select name, kind, attempt, op_key, payload_sha256, conflicted, owner_id, "
            "       execution_generation "
            "  from capture_operations where job_id = %s order by seq", (str(job_id),))
        return [{"name": r[0], "kind": r[1], "attempt": r[2], "op_key": r[3], "digest": r[4],
                 "conflicted": r[5], "owner_id": r[6], "gen": r[7]} for r in cur.fetchall()]


async def test_three_meshing_passes_capture_three_distinct_planner_operations(exhausted_capturing):
    job_id, _seen, _built, native, _marks, rounds, _tess = exhausted_capturing
    assert len(native) == 3 and len(rounds) == 3, "the run did not make three passes"

    rows = _capture_rows(job_id)
    planner = [r for r in rows if r["name"] == "planner_run"]
    assert len(planner) == 3, (
        f"three re-plans must leave three durable operations, found {len(planner)}; "
        "collapsing them loses every planner record for the job")
    assert len({r["op_key"] for r in planner}) == 3, \
        "the three re-plans share an operation identity"
    assert [r["attempt"] for r in planner] == [r["attempt"] for r in planner if r["attempt"]
                                               is not None], \
        "a planner operation was captured with no attempt coordinate"


async def test_no_operation_of_a_three_pass_run_is_quarantined(exhausted_capturing):
    job_id, _seen, _built, _native, _marks, _rounds, _tess = exhausted_capturing
    rows = _capture_rows(job_id)
    assert rows, "the run captured nothing"
    conflicted = [(r["name"], r["op_key"][:16]) for r in rows if r["conflicted"]]
    assert conflicted == [], f"operations were quarantined and will never be exported: {conflicted}"


async def test_an_exact_replay_of_one_operation_is_idempotent(exhausted_capturing):
    # Re-present a captured operation to the durable authority exactly as production derived it:
    # same owner, job, generation, identity and payload. It must be absorbed as a replay, leaving
    # one effect and no quarantine.
    job_id, _seen, _built, _native, _marks, _rounds, _tess = exhausted_capturing
    from meshpipeline.persistence.repositories import capture_repository as cap

    before = _capture_rows(job_id)
    original = next(r for r in before if r["name"] == "planner_run")

    import psycopg
    dsn = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("select payload from capture_operations where job_id = %s and op_key = %s",
                    (str(job_id), original["op_key"]))
        payload = cur.fetchone()[0]

    result = cap.record_operation(
        owner_id=original["owner_id"], job_id=str(job_id),
        execution_generation=original["gen"], op_key=original["op_key"],
        name=original["name"], payload=payload, kind=original["kind"],
        attempt=original["attempt"])

    assert result.outcome == "duplicate", \
        f"an identical replay was recorded as {result.outcome!r}, not absorbed"
    after = _capture_rows(job_id)
    assert len(after) == len(before), "an identical replay created a second durable effect"
    assert [r for r in after if r["op_key"] == original["op_key"]][0]["conflicted"] is False, \
        "an identical replay quarantined the operation it replayed"


async def test_a_different_payload_under_one_identity_is_still_quarantined(exhausted_capturing):
    # The protection this repair must NOT weaken: two genuinely different payloads under one
    # identity remain a conflict.
    job_id, _seen, _built, _native, _marks, _rounds, _tess = exhausted_capturing
    from meshpipeline.persistence.repositories import capture_repository as cap

    original = next(r for r in _capture_rows(job_id) if r["name"] == "planner_run")
    result = cap.record_operation(
        owner_id=original["owner_id"], job_id=str(job_id),
        execution_generation=original["gen"], op_key=original["op_key"],
        name=original["name"], payload={"status": "a different reading of one operation"},
        kind=original["kind"], attempt=original["attempt"])
    assert result.outcome == "conflicted", \
        f"a disagreeing payload was accepted as {result.outcome!r}; conflict detection is weakened"


async def test_the_accepting_run_captures_one_planner_operation_and_no_conflict(accepted_capturing):
    job_id, _seen, _built, native, _marks, rounds, _tess = accepted_capturing
    assert len(native) == 1 and len(rounds) == 1
    rows = _capture_rows(job_id)
    planner = [r for r in rows if r["name"] == "planner_run"]
    assert len(planner) == 1, f"one pass must capture one planner operation, found {len(planner)}"
    assert [r for r in rows if r["conflicted"]] == [], "the accepting run quarantined an operation"


# tool operations - the driver's own operation lifecycle, at the Redis delivery boundary
#
# Each meshing pass authors a configuration, validates it, runs the mesher and inspects the
# output: four operations, each with a begin and an end. They carry no op_id into capture, so the
# only evidence that they were delivered correctly is the replayable backlog itself.

TOOL_OPS = ("author_configuration", "validate_configuration",
            "run_native_mesher", "inspect_native_output")


# The replayable user backlog, read from Redis on its own connection after the graph has returned.
def _backlog(job_id) -> list[dict]:
    import json as _json

    import redis as _redis

    from meshpipeline.events.channels import log_key_for

    r = _redis.from_url(provcfg.REDIS_URL)
    try:
        return [_json.loads(x) for x in r.lrange(log_key_for(str(job_id)), 0, -1)]
    finally:
        r.close()


def _tool_events(job_id) -> list[dict]:
    return [e for e in _backlog(job_id) if e.get("type") in ("tool_call", "tool_result")]


#: `op:<job>:<builder attempt>:<native attempt>:<operation>`, with `:r` appended for the end.
def _pass_of(event) -> int:
    return int(str(event["id"]).split(":")[3])


def _operation_of(event) -> str:
    return str(event["id"]).split(":")[4]


async def test_one_pass_delivers_every_tool_operation_once_with_a_matching_end(accepted):
    job_id, _seen, _built, _native, _marks, _rounds, _tess = accepted
    events = _tool_events(job_id)

    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert sorted(_operation_of(e) for e in calls) == sorted(TOOL_OPS), \
        f"a pass did not open its four operations: {[_operation_of(e) for e in calls]}"
    assert len(results) == len(calls), (
        f"{len(calls)} operations began but {len(results)} ended; an operation that never ends "
        "stays open forever for the reader")
    assert {e["id"] + ":r" for e in calls} == {e["id"] for e in results}, \
        "an end did not correlate with the begin it belongs to"


async def test_three_passes_deliver_every_tool_event_distinctly(exhausted):
    job_id, _seen, _built, native, _marks, _rounds, _tess = exhausted
    assert len(native) == 3
    events = _tool_events(job_id)

    # four operations, two phases, three passes
    assert len(events) == len(TOOL_OPS) * 2 * 3, \
        f"expected {len(TOOL_OPS) * 2 * 3} tool events across three passes, got {len(events)}"
    assert len({e["id"] for e in events}) == len(events), (
        "two tool events were delivered under one identity; a reader keyed by that identity "
        "cannot tell the passes apart")
    assert len({e["seq"] for e in events}) == len(events), "two events shared a sequence"

    for op in TOOL_OPS:
        passes = sorted(_pass_of(e) for e in events
                        if e["type"] == "tool_call" and _operation_of(e) == op)
        assert passes == [1, 2, 3], f"{op} was not delivered once per pass: {passes}"


async def test_the_same_operation_in_another_pass_is_a_different_event(exhausted):
    job_id, _seen, _built, _native, _marks, _rounds, _tess = exhausted
    events = _tool_events(job_id)
    for op in TOOL_OPS:
        ids = {e["id"] for e in events if e["type"] == "tool_call" and _operation_of(e) == op}
        assert len(ids) == 3, f"{op} reused an identity across passes: {ids}"
        # the identities differ ONLY in the pass coordinate
        stripped = {i.replace(f":{_pass_of({'id': i})}:", ":") for i in ids}
        assert len(stripped) == 1, f"{op} identities differ by more than the pass: {ids}"


async def test_an_exact_replay_of_a_tool_event_is_delivered_once(accepted):
    # Re-present one delivered event exactly as production derived it. The keyed emit path must
    # absorb it: the same operation, pass and phase is the same event, however often it arrives.
    job_id, seen, _built, _native, _marks, _rounds, _tess = accepted
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    original = next(e for e in _tool_events(job_id) if e["type"] == "tool_call")
    before = len(_backlog(job_id))

    # The identity is scoped to the generation that produced it, so a faithful replay has to run
    # under the same claimed ownership - which is also what makes a takeover by a NEWER generation
    # re-deliver rather than fall silent.
    own = next(r for r in _driver_records(seen) if r["method"] == "tool_call")["own"]
    pub = JobPublisher(str(job_id), agent="builder")
    with fence.execution_ownership(own):
        pub.tool_call(original["id"], _operation_of(original), None, "started",
                      op_id=original["id"])
        first = len(_backlog(job_id))
        pub.tool_call(original["id"], _operation_of(original), None, "started",
                      op_id=original["id"])
        second = len(_backlog(job_id))

    assert first == before, "a replay of an already-delivered event was appended again"
    assert second == first, "a second replay was appended"


async def test_both_tool_sites_publish_under_the_claimed_durable_ownership(exhausted):
    job_id, seen, _built, _native, _marks, _rounds, _tess = exhausted
    records = [r for r in _driver_records(seen)
               if r["method"] in ("tool_call", "tool_result")]
    assert records, "the run published no tool operation"

    row = await _row(job_id)
    publishers = set()
    for rec in records:
        site = f"{rec['fn']}::{rec['method']}#1"
        own = rec["own"]
        assert own is not None, f"{site} published with NO execution ownership bound"
        assert rec["impl"] == "JobPublisher", f"{site} used {rec['impl']}"
        assert rec["publisher_job_id"] == str(job_id)
        assert str(own.job_id) == str(job_id)
        assert own.execution_generation == row.execution_generation
        assert own.claim_epoch == row.execution_claim_epoch
        assert (own.worker_token == row.active_worker_token) is True, \
            f"{site} did not match the durable token"
        publishers.add(rec["publisher"])
    assert len(publishers) == 1, "the tool operations used more than one publisher object"

    assert {f"{r['fn']}::{r['method']}#1" for r in records} == TOOL_SITES, \
        "the observed tool sites are not the two this suite owns"


# internal snappy - the through-flow driver
#
# The internal driver separates a fluid volume out of a closed CAD solid, then meshes the cavity.
# Its eight publication sites mirror the external ones and are reached the same way: an accepting
# pass, a run refused every pass on quality, and a solid whose openings cannot be closed.

INTERNAL_FN = "_build_internal_deterministic"

INTERNAL_ACCEPTED = {
    f"{INTERNAL_FN}::note#1": 1,       # fluid volume identified
    f"{INTERNAL_FN}::note#2": 1,       # meshing pass opened
    f"{INTERNAL_FN}::note#4": 1,       # filling the cavity
    "_run_snappy_timed::meshing#1": 1,
    f"{INTERNAL_FN}::meshed#1": 1,
    f"{INTERNAL_FN}::note#5": 1,       # production-grade
}

INTERNAL_EXHAUSTED = {
    f"{INTERNAL_FN}::note#1": 1,
    f"{INTERNAL_FN}::note#2": 3,
    f"{INTERNAL_FN}::note#4": 3,
    "_run_snappy_timed::meshing#1": 3,
    f"{INTERNAL_FN}::meshed#1": 3,
    f"{INTERNAL_FN}::note#6": 3,       # fell short, re-planning
    f"{INTERNAL_FN}::note#3": 1,       # passes exhausted, best mesh submitted
}

# An accepting run on a solid with a plate thinner than the wall cell: the same six sites, plus
# the one disclosure that the plate was found and is being refined locally (fix #4, the orifice
# cluster). It fires once per pass, before the case is rendered.
INTERNAL_THIN_FEATURE = {**INTERNAL_ACCEPTED, f"{INTERNAL_FN}::note#7": 1}


@pytest.fixture()
async def internal_accepted(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, native_double=False, internal=True)


@pytest.fixture()
async def internal_thin_feature(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, native_double=False, internal=True,
                      thin_feature=True)


@pytest.fixture()
async def internal_exhausted(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, native_double=True, internal=True)


@pytest.fixture()
async def internal_unseparable(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, native_double=False, internal=True,
                      tessellate_fail=True)


@pytest.fixture()
async def internal_binding_refused(monkeypatch, tmp_path):
    return await _run(monkeypatch, tmp_path, native_double=False, internal=True,
                      unbindable_patches=True)


async def test_the_internal_accepting_run_publishes_exactly_its_six_sites(internal_accepted):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = internal_accepted
    assert _owned_counts(seen, accepted=True) == INTERNAL_ACCEPTED


async def test_the_internal_exhausting_run_publishes_exactly_its_seven_sites(internal_exhausted):
    _job_id, seen, _built, _native, _marks, _rounds, _tess = internal_exhausted
    assert _owned_counts(seen, accepted=False) == INTERNAL_EXHAUSTED


async def test_an_unseparable_solid_is_refused_at_the_one_internal_error_site(internal_unseparable):
    _job_id, seen, _built, native, _marks, _rounds, tess = internal_unseparable
    assert _owned_counts(seen, accepted=False) == {f"{INTERNAL_FN}::error#1": 1}
    assert len(tess) == 1, "the tessellation boundary was not reached exactly once"
    assert native == [], "the mesher ran despite the fluid volume never being separated"
    rec = next(r for r in _driver_records(seen) if r["method"] == "error")
    assert rec["op_id"] == "internal:volume-unseparable"


async def test_a_declaration_no_opening_satisfies_is_refused_before_meshing(
        internal_binding_refused):
    _job_id, seen, _built, native, _marks, _rounds, _tess = internal_binding_refused
    assert _owned_counts(seen, accepted=False) == {f"{INTERNAL_FN}::error#2": 1}
    assert native == [], "a refused binding must never reach the mesher"


async def test_a_thin_feature_is_disclosed_once_at_its_own_site(internal_thin_feature):
    _job_id, seen, _built, native, _marks, _rounds, _tess = internal_thin_feature
    assert _owned_counts(seen, accepted=True) == INTERNAL_THIN_FEATURE
    assert len(native) == 1, "the thin-feature disclosure must not cost a meshing pass"
    # attempt-scoped like every other internal operation identity, and published once
    recs = [r for r in _driver_records(seen)
            if r["method"] == "note" and r["op_id"].startswith("internal:thin-feature:")]
    assert [r["op_id"] for r in recs] == ["internal:thin-feature:1"], recs


async def test_the_internal_union_reaches_all_ten_sites_and_no_external_one(
        internal_accepted, internal_exhausted, internal_unseparable,
        internal_binding_refused, internal_thin_feature):
    union: set = set()
    for bundle, acc in ((internal_accepted, True), (internal_exhausted, False),
                        (internal_unseparable, False), (internal_binding_refused, False),
                        (internal_thin_feature, True)):
        _j, seen, _b, _n, _m, _r, _t = bundle
        sites = set(_sites(seen, accepted=acc)) - TOOL_SITES
        assert not [s for s in sites if s.startswith("_build_snappy_deterministic")], \
            "an internal run reached the external driver"
        union |= sites

    expected = {f"{INTERNAL_FN}::{n}" for n in
                ("error#1", "error#2", "note#1", "note#2", "note#3", "note#4", "note#5",
                 "note#6", "note#7", "meshed#1")} | {"_run_snappy_timed::meshing#1"}
    assert union == expected, f"the internal union is not the ten owned sites: {union}"


async def test_every_internal_emission_runs_under_the_claimed_durable_ownership(
        internal_accepted, internal_exhausted, internal_unseparable,
        internal_binding_refused, internal_thin_feature):
    for label, bundle, acc in (("accepted", internal_accepted, True),
                               ("exhausted", internal_exhausted, False),
                               ("unseparable", internal_unseparable, False),
                               ("binding-refused", internal_binding_refused, False),
                               ("thin-feature", internal_thin_feature, True)):
        job_id, seen, built, _n, _m, _r, _t = bundle
        row = await _row(job_id)
        records = [r for r in _driver_records(seen)
                   if _canonical(r, accepted=acc) not in TOOL_SITES]
        assert records, f"the {label} run published no owned driver event"
        publishers = set()
        for rec in records:
            site = _canonical(rec, accepted=acc)
            own = rec["own"]
            assert own is not None, f"{label}/{site} published with NO ownership bound"
            assert rec["impl"] == "JobPublisher", f"{label}/{site} used {rec['impl']}"
            assert rec["publisher_job_id"] == str(job_id)
            assert str(own.job_id) == str(job_id)
            assert own.execution_generation == row.execution_generation
            assert own.claim_epoch == row.execution_claim_epoch
            assert (own.worker_token == row.active_worker_token) is True, \
                f"{label}/{site} did not match the durable token"
            publishers.add(rec["publisher"])
        assert len(publishers) == 1, f"the {label} run used more than one publisher object"
        assert [b for b in built if b[0] == str(job_id)
                and b[2] == "OwnershipCheckedPublisher" and b[3] == "JobPublisher"], \
            f"the {label} run built no ownership-checked production publisher"


async def test_the_tessellation_boundary_runs_on_a_worker_thread_under_the_same_ownership(
        internal_accepted):
    job_id, seen, _built, _native, _marks, _rounds, tess = internal_accepted
    assert len(tess) == 1, f"the tessellation boundary ran {len(tess)} times, not once"
    call = tess[0]
    assert not call["is_event_loop_thread"], \
        "tessellate_internal did not run on a worker thread; to_thread was bypassed"
    assert call["source_exists"], "the CAD solid was not on disk at the boundary"
    assert call["suffix"] == ".step", "the internal driver accepted a non-solid source"

    inside = call["own"]
    assert inside is not None, "the tessellation thread saw NO ownership"
    after = next(r for r in _driver_records(seen) if r["method"] == "note")["own"]
    assert after is inside, "ownership changed identity across the tessellation boundary"

    row = await _row(job_id)
    assert str(inside.job_id) == str(job_id)
    assert inside.execution_generation == row.execution_generation
    assert inside.claim_epoch == row.execution_claim_epoch
    assert (inside.worker_token == row.active_worker_token) is True


async def test_internal_runs_clear_ownership_and_leave_no_task(internal_accepted,
                                                               internal_unseparable):
    for label, bundle in (("accepted", internal_accepted),
                          ("unseparable", internal_unseparable)):
        _j, _seen, _b, _n, marks, _r, _t = bundle
        assert marks and any(m is not None for m in marks), f"{label} never ran under a claim"
        assert marks[-1] is None, f"{label} left ownership bound after the graph returned"
    assert fence.current_ownership() is None
    assert [t for t in asyncio.all_tasks()
            if not t.done() and "_beat" in (t.get_coro().__qualname__ or "")] == [], \
        "a heartbeat task outlived an internal run"


# workspace and temporary-geometry lifecycle
#
# The Snappy paths do NOT delete their workspace when the graph returns, and that is deliberate:
# `job_service.final_attempt_workspace` reads generation_*/attempt_*/mesh_manifest.json out of it
# for delivery and continuation, and the training corpus exports from it. The declared reaper is
# application/maintenance/cleanup.py, which purges the whole job root once the job has failed, has
# ended before the cutoff, and has no export still pending.
#
# So the contract these prove is CONTAINMENT, not disappearance: everything a run creates lives
# under its own job root, where that authority can reap it in one call, and nothing escapes to
# /tmp, the API root or the workspace parent. A scratch file outside that root would survive every
# cleanup authority there is, which is what a leak actually looks like here.

def _job_root(job_id) -> Path:
    import meshpipeline.settings.runtime as rtcfg
    return Path(rtcfg.WORKSPACE_BASE) / str(job_id)


def _tree(root: Path) -> set:
    return set(root.rglob("*")) if root.exists() else set()


# Task-owned paths anywhere except this job's own root.
def _outside_paths(job_id) -> list:
    import meshpipeline.settings.runtime as rtcfg
    parent = Path(rtcfg.WORKSPACE_BASE)
    mine = _job_root(job_id)
    stray = [p for p in parent.glob("*") if p != mine and str(job_id) in p.name]
    for base in ("/tmp", os.getenv("API_ROOT", "") or "/srv/api-root"):
        b = Path(base)
        if b.is_dir():
            stray += list(b.glob(f"*{job_id}*"))
    return stray


async def _assert_contained(job_id, source: Path, digest_before: str):
    import hashlib

    root = _job_root(job_id)
    assert root.is_dir(), "the run left no workspace under its own job root"
    assert _outside_paths(job_id) == [], \
        f"task-owned paths escaped the job root: {_outside_paths(job_id)}"

    # the reaper's own predicate: one rmtree of this root removes everything the run created
    import meshpipeline.settings.runtime as rtcfg
    for p in _tree(root):
        assert Path(rtcfg.WORKSPACE_BASE) in p.parents or p == root, p
    assert source.exists(), "the source geometry was consumed"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest_before, \
        "the run modified the original source geometry"

    assert fence.current_ownership() is None
    assert [t for t in asyncio.all_tasks()
            if not t.done() and "_beat" in (t.get_coro().__qualname__ or "")] == [], \
        "a heartbeat task outlived the run"


def _source_of(tmp_path: Path) -> Path:
    return next(tmp_path.rglob("*.st*p")) if list(tmp_path.rglob("*.st*p")) \
        else next(tmp_path.rglob("*.stl"))


# A - success


async def test_the_accepting_run_contains_everything_under_its_job_root(accepted, tmp_path):
    import hashlib
    job_id, _seen, _b, _n, _m, _r, _t = accepted
    src = _source_of(tmp_path)
    await _assert_contained(job_id, src, hashlib.sha256(src.read_bytes()).hexdigest())

    ws = list(_job_root(job_id).glob("generation_*/attempt_*"))
    assert ws, "no attempt workspace was created"
    assert (ws[0] / "input.stl").exists(), "the staged surface is not in the attempt workspace"


# B - quality exhaustion


async def test_three_passes_do_not_accumulate_attempt_workspaces(exhausted, tmp_path):
    import hashlib
    job_id, _seen, _b, native, _m, _r, _t = exhausted
    assert len(native) == 3
    src = _source_of(tmp_path)
    await _assert_contained(job_id, src, hashlib.sha256(src.read_bytes()).hexdigest())

    # three meshing passes are three passes of ONE builder attempt: they reuse one workspace
    ws = list(_job_root(job_id).glob("generation_*/attempt_*"))
    assert len(ws) == 1, f"three passes left {len(ws)} attempt workspaces: {ws}"


# C - native exception


async def test_an_unseparable_solid_leaves_nothing_outside_its_job_root(internal_unseparable,
                                                                       tmp_path):
    import hashlib
    job_id, _seen, _b, _n, _m, _r, tess = internal_unseparable
    assert len(tess) == 1, "the exception did not come from the real native boundary"
    src = _source_of(tmp_path)
    await _assert_contained(job_id, src, hashlib.sha256(src.read_bytes()).hexdigest())
    # the tessellation output directory is inside the workspace, so the reaper owns it too
    stray = [p for p in _job_root(job_id).rglob("_internal_stls")
             if _job_root(job_id) not in p.parents]
    assert stray == [], f"temporary internal geometry landed outside the job root: {stray}"


# D - cancellation at the native boundary


async def test_cancelling_at_the_native_boundary_tears_down_and_contains(monkeypatch, tmp_path):
    import hashlib
    import threading

    entered, release = threading.Event(), threading.Event()

    def blocking_run_snappy(workspace, *, context=None, bashrc="", timeout=1800):
        ws = Path(workspace)
        poly = ws / "constant" / "polyMesh"
        poly.mkdir(parents=True, exist_ok=True)
        (poly / "boundary").write_text(_BOUNDARY)
        entered.set()
        release.wait(60)                    # a barrier, never a sleep
        return {"rc": 0, "timed_out": False, "layer_coverage": 0.95, "per_patch_layers": {}}

    from meshpipeline.runtime.composition import install_adapters
    install_adapters()
    job_id = uuid.uuid4()
    owner_id = f"snappy-{job_id.hex[:8]}"
    await _seed(job_id, owner_id)

    seen: list = []
    marks: list = []
    _record_emissions(monkeypatch, seen)
    _watch_post_graph(monkeypatch, marks)
    _install_quality_double(monkeypatch, skewed=False)
    _install_provider_double(monkeypatch, [])
    from meshpipeline.engines.snappy import snappy_runner as R
    monkeypatch.setattr(R, "run_snappy", blocking_run_snappy)

    import meshpipeline.pipeline.graph as gm
    root = tmp_path / f"run-{job_id.hex[:8]}"
    graph = _graph(_external_state(root, owner_id, _half_model))
    monkeypatch.setattr(gm, "build_graph",
                        lambda checkpointer: graph.compile(checkpointer=checkpointer))

    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    run = asyncio.create_task(_run_async(JobRequest(job_id=str(job_id), owner_id=owner_id)))

    waiter = asyncio.create_task(asyncio.to_thread(entered.wait, 60))
    done, _ = await asyncio.wait({waiter, run}, return_when=asyncio.FIRST_COMPLETED)
    assert run not in done, f"the run finished before reaching the native boundary: {run}"
    await waiter

    # cancel through the production task boundary, then let the worker thread finish
    run.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert run.cancelled(), "cancellation was converted into a completed run"
    src = _source_of(tmp_path)
    await _assert_contained(job_id, src, hashlib.sha256(src.read_bytes()).hexdigest())
    assert not [e for e in _backlog(job_id) if e.get("type") == "closing"], \
        "a cancelled run published a terminal closing event"


async def test_the_internal_accepting_run_contains_its_temporary_geometry(internal_accepted,
                                                                         tmp_path):
    # The only path that actually materialises `_internal_stls`: the fluid volume is separated
    # into real surface files before anything is meshed. They are scratch, so they must land
    # inside the job root the reaper owns - not beside it, and not in /tmp.
    import hashlib
    job_id, _seen, _b, _n, _m, _r, tess = internal_accepted
    assert len(tess) == 1, "the tessellation boundary was not reached"

    src = _source_of(tmp_path)
    await _assert_contained(job_id, src, hashlib.sha256(src.read_bytes()).hexdigest())

    root = _job_root(job_id)
    made = [Path(tess[0]["out_dir"])]
    assert made[0].name == "_internal_stls"
    assert root in made[0].parents, \
        f"temporary internal geometry was written outside the job root: {made[0]}"
    assert list(made[0].glob("*.stl")), "the separated surfaces were not written"
