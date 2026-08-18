# Responsibility: Verify the engine-selection and geometry-admission publications run under the claimed execution.
# Boundaries: those two pipeline nodes; the executor node and the builder loop are their own suites.
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest
from tests._geometry_support import materialized
from tests.integration import execution_ownership_support as ownership

from meshpipeline.application import execution_fence as fence
from meshpipeline.contracts.event_stream import StaleExecutionPublish

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)
pytest.importorskip("pyvista")

SELECT_MOD = "pipeline/engine_select.py"
ADMISSION_MOD = "pipeline/geometry_admission.py"
#: The shared rationale authority admission publishes its conclusion through. It is its own
#: root - the site is in rationale.py, not in the node - and it is recorded to prove the node
#: reaches it, never counted as one of the node's own sites.
RATIONALE_MOD = "contracts/rationale.py"

#: How an event TRAVELS. Every publication below reaches the adapter through the ownership gate,
#: so the immediate caller is always the gated wrapper, never the site that decided to publish.
_TRANSPARENT = {"execution_publisher.py"}

#: A complete, valid DECLARED context for vmtk, so the node sees no spurious declared rejection
#: and what it acts on is the measured surface. Mirrors what intake threads into state.
_VMTK = {
    "engine": "vmtk", "purpose": "internal_cfd",
    "input_kind": "body-surface", "dimensionality": "3D",
    "intake_patches": [{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                       {"name": "outlet", "type": "outlet"}],
    "engine_params": {"wall_layers": "on"},
}


def _record(monkeypatch, seen: list) -> None:
    # Records who asked the REAL adapter to publish. Nothing is replaced: the production method
    # always runs, so what is measured is a real Redis write under a real claim.
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    root = Path(sys.modules["meshpipeline"].__file__).parent

    def wrap(name: str):
        original = getattr(JobPublisher, name)

        def w(self, *a, **k):
            frame = sys._getframe(1)
            while frame is not None and Path(frame.f_code.co_filename).name in _TRANSPARENT:
                frame = frame.f_back
            try:
                rel = Path(frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            except ValueError:
                rel = ""
            if rel in (SELECT_MOD, ADMISSION_MOD, RATIONALE_MOD):
                seen.append({"module": rel, "fn": frame.f_code.co_name, "method": name,
                             "op_id": k.get("op_id", ""), "impl": type(self).__name__,
                             "own": fence.current_ownership(),
                             "job": str(getattr(self, "job_id", "")), "args": a})
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, name, w)

    for method in ("stage", "note", "rationale"):
        wrap(method)


def _redis_state(job_id) -> dict:
    from meshpipeline.adapters.event_stream.redis import sync_client
    from meshpipeline.events.channels import (
        fence_key_for,
        log_key_for,
        opkey_set_for,
        seq_key_for,
    )

    r = sync_client()
    jid = str(job_id)
    return {"seq": r.get(seq_key_for(jid)),
            "backlog": list(r.lrange(log_key_for(jid), 0, -1)),
            "opkeys": set(r.smembers(opkey_set_for(jid))),
            "fence": r.get(fence_key_for(jid))}


def _self_intersecting_geometry(tmp_path: Path):
    # A REAL defect, measured by the real check: two triangles that pierce each other. Nothing
    # below the admission boundary is doubled - the engine's own contract does the rejecting.
    import pyvista as pv

    geometry = materialized(tmp_path, filename="lumen.stl")
    points = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0],
                       [0.5, 0.5, -1], [0.5, 0.5, 1], [1.5, 0.5, 0]], dtype=float)
    faces = np.hstack([[3, 0, 1, 2], [3, 3, 4, 5]])
    pv.PolyData(points, faces).extract_surface().triangulate().save(str(geometry.local_path))
    return geometry.to_state()


async def _claimed():
    # The composition root binds the real Redis-backed publisher factory. Without it the node
    # publishes into the default no-op port and the run looks green while nothing was written.
    from meshpipeline.runtime.composition import install_adapters

    install_adapters()
    return await ownership.seeded_claim("node")


async def _owned(monkeypatch, seen: list, node, state_fn):
    # A real claim, the real gate, the real adapter: the only thing this adds is the recorder.
    job_id, own, Session, engine = await _claimed()
    _record(monkeypatch, seen)
    try:
        with fence.execution_ownership(own, session_factory=Session):
            out = await node(state_fn(job_id))
        return {"job_id": job_id, "own": own, "out": out, "sessions": Session}
    finally:
        await engine.dispose()


# engine selection


@pytest.fixture()
async def forced(monkeypatch):
    from meshpipeline.pipeline.engine_select import node_engine_select

    seen: list = []
    res = await _owned(monkeypatch, seen, node_engine_select,
                       lambda j: {"job_id": str(j), "purpose": "internal_cfd"})
    res["seen"] = seen
    yield res


@pytest.fixture()
async def resolved(monkeypatch):
    from meshpipeline.pipeline.engine_select import node_engine_select

    seen: list = []
    res = await _owned(monkeypatch, seen, node_engine_select, lambda j: {"job_id": str(j)})
    res["seen"] = seen
    yield res


async def test_the_internal_topology_selection_publishes_under_the_claim(forced):
    seen = forced["seen"]
    assert [(r["fn"], r["method"]) for r in seen] == [("_publish", "stage"), ("_publish", "note")], \
        f"engine selection published {[(r['fn'], r['method']) for r in seen]}"
    assert forced["out"].get("engine"), "no engine was selected"
    for r in seen:
        assert r["module"] == SELECT_MOD and r["impl"] == "JobPublisher"
        assert r["own"] is not None, f"{r['fn']}.{r['method']} published with NO ownership bound"
        assert str(r["own"].job_id) == str(forced["job_id"])
        assert r["own"].execution_generation == forced["own"].execution_generation
        assert r["op_id"] == "forced"


async def test_the_default_selection_publishes_the_same_two_sites(resolved):
    seen = resolved["seen"]
    assert [(r["fn"], r["method"]) for r in seen] == [("_publish", "stage"), ("_publish", "note")]
    assert {r["op_id"] for r in seen} == {"resolved"}
    assert resolved["out"].get("engine"), "no engine was selected"


async def test_the_selection_the_user_pinned_is_never_re_decided(monkeypatch):
    # The product's policy: a pinned engine is used as pinned. The node announces nothing,
    # recommends nothing and falls back to nothing.
    from meshpipeline.pipeline.engine_select import node_engine_select

    seen: list = []
    res = await _owned(monkeypatch, seen, node_engine_select,
                       lambda j: {"job_id": str(j), "engine": "gmsh", "purpose": "internal_cfd"})
    assert res["out"] == {}, f"a pinned engine was re-decided into {res['out']}"
    assert seen == [], f"a pinned selection published {seen}"


# geometry admission


@pytest.fixture()
async def rejected(monkeypatch, tmp_path):
    from meshpipeline.pipeline.geometry_admission import node_geometry_admission

    seen: list = []
    geometry = _self_intersecting_geometry(tmp_path)
    res = await _owned(monkeypatch, seen, node_geometry_admission,
                       lambda j: {**_VMTK, "job_id": str(j), "geometry": geometry})
    res["seen"] = seen
    yield res


async def test_a_measured_rejection_publishes_under_the_claim(rejected):
    seen = rejected["seen"]
    assert rejected["out"].get("geometry_unsuitable_reason", "").startswith("[GEOMETRY_UNSUITABLE]"), \
        f"the real self-intersecting surface was admitted: {rejected['out']}"
    sites = [(r["fn"], r["method"]) for r in seen]
    assert sites[:2] == [("_publish", "stage"), ("_publish", "note")], sites
    for r in seen:
        assert r["impl"] == "JobPublisher"
        assert r["own"] is not None, f"{r['fn']}.{r['method']} published with NO ownership bound"
        assert str(r["own"].job_id) == str(rejected["job_id"])
        assert r["own"].execution_generation == rejected["own"].execution_generation


async def test_the_rejection_reaches_the_user_as_an_error_beside_its_conclusion(rejected):
    published = [r for r in rejected["seen"] if r["module"] == ADMISSION_MOD]
    note = next(r for r in published if r["method"] == "note")
    assert note["args"][1] == "error", "the rejection was not surfaced as an error"
    assert "self-intersect" in note["args"][0]
    # the application's own conclusion travels beside the message, published from the SHARED
    # rationale authority - the node hands it the publisher rather than owning a site of its own
    conclusion = [r for r in rejected["seen"] if r["method"] == "rationale"]
    assert conclusion, "the admission verdict published no rationale"
    assert conclusion[0]["module"] == RATIONALE_MOD and conclusion[0]["fn"] == "_asay", \
        f"the conclusion came from {conclusion[0]['module']}::{conclusion[0]['fn']}"


# stale controls: one behavioural refusal per production root


async def _stale_refusal(monkeypatch, node, state_fn, *, expected):
    job_id, own, Session, engine = await _claimed()
    seen: list = []
    _record(monkeypatch, seen)
    try:
        before = _redis_state(job_id)
        assert before["fence"], "the claim installed no fence to defend"
        stale = ownership.superseded(job_id, own)
        with fence.execution_ownership(stale, session_factory=Session):
            with pytest.raises(expected):
                await node(state_fn(job_id))
        after = _redis_state(job_id)
    finally:
        await engine.dispose()
    return {"job_id": job_id, "seen": seen, "before": before, "after": after}


def _assert_nothing_moved(result) -> None:
    before, after = result["before"], result["after"]
    assert result["seen"] == [], \
        f"a superseded generation published {[(r['fn'], r['method']) for r in result['seen']]}"
    assert after["seq"] == before["seq"], "a stale attempt advanced the event sequence"
    assert after["backlog"] == before["backlog"], "a stale attempt reached the durable backlog"
    assert after["opkeys"] == before["opkeys"], "a stale attempt recorded an operation key"
    assert after["fence"] == before["fence"], \
        "a stale attempt disturbed the CURRENT worker's fence"


async def test_a_superseded_generation_selects_no_engine_and_writes_no_event(monkeypatch):
    from meshpipeline.pipeline.engine_select import node_engine_select

    result = await _stale_refusal(monkeypatch, node_engine_select,
                                  lambda j: {"job_id": str(j), "purpose": "internal_cfd"},
                                  expected=StaleExecutionPublish)
    _assert_nothing_moved(result)


async def test_a_superseded_generation_admits_no_geometry_and_writes_no_event(
        monkeypatch, tmp_path):
    from meshpipeline.pipeline.geometry_admission import node_geometry_admission

    geometry = _self_intersecting_geometry(tmp_path)
    result = await _stale_refusal(monkeypatch, node_geometry_admission,
                                  lambda j: {**_VMTK, "job_id": str(j), "geometry": geometry},
                                  expected=StaleExecutionPublish)
    _assert_nothing_moved(result)


async def test_a_superseded_generation_runs_no_executor_gate_and_writes_no_event(
        monkeypatch, tmp_path):
    # The executor fences BEFORE it builds its publisher, so the refusal is StaleWorkerFenced and
    # no publication is ever attempted - the same durable outcome, reached one seam earlier.
    from meshpipeline.application.execution_fence import StaleWorkerFenced
    from meshpipeline.pipeline.executor import node_executor

    result = await _stale_refusal(
        monkeypatch, node_executor,
        lambda j: {"job_id": str(j), "openfoam_workspace": str(tmp_path), "engine": "gmsh",
                   "retry_count": 0},
        expected=StaleWorkerFenced)
    _assert_nothing_moved(result)


async def test_the_current_owner_still_publishes_after_a_stale_attempt(monkeypatch, tmp_path):
    # The refusal must not have cost the legitimate worker anything: same job, same claim, the
    # very next publication is accepted and the backlog grows by exactly what it wrote.
    from meshpipeline.pipeline.engine_select import node_engine_select

    job_id, own, Session, engine = await _claimed()
    seen: list = []
    _record(monkeypatch, seen)
    try:
        stale = ownership.superseded(job_id, own)
        with fence.execution_ownership(stale, session_factory=Session):
            with pytest.raises(StaleExecutionPublish):
                await node_engine_select({"job_id": str(job_id), "purpose": "internal_cfd"})
        refused_at = _redis_state(job_id)
        with fence.execution_ownership(own, session_factory=Session):
            out = await node_engine_select({"job_id": str(job_id), "purpose": "internal_cfd"})
        after = _redis_state(job_id)
    finally:
        await engine.dispose()

    assert out.get("engine"), "the current owner's selection did not complete"
    assert [(r["fn"], r["method"]) for r in seen] == [("_publish", "stage"), ("_publish", "note")]
    assert len(after["backlog"]) == len(refused_at["backlog"]) + 2, \
        "the current owner's two events did not reach the backlog"
    assert after["fence"] == refused_at["fence"], "the current worker's fence changed"


# the ordinals the canonical identities are built from


async def test_each_root_publishes_its_two_sites_from_one_function(forced, rejected):
    # Both roots publish from a single `_publish` helper, which is what makes their canonical
    # identities `_publish::stage#1` and `_publish::note#1` rather than one per caller.
    for res, module in ((forced, SELECT_MOD), (rejected, ADMISSION_MOD)):
        owned = [r for r in res["seen"] if r["module"] == module]
        assert {r["fn"] for r in owned} == {"_publish"}, \
            f"{module} published from more than one function: {[r['fn'] for r in owned]}"
        assert [r["method"] for r in owned][:2] == ["stage", "note"]


async def test_every_recorded_publication_used_a_real_uuid_job(forced, resolved, rejected):
    for res in (forced, resolved, rejected):
        for r in res["seen"]:
            uuid.UUID(r["job"])          # raises if the publisher was built for a placeholder
