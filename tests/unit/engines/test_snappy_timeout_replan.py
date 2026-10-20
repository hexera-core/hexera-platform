# Responsibility: Verify a mesh run that ran out of time is reported as a timeout, and that a snappy pass its guard clock stops is a guard trip - never a coarser re-plan.
# Boundaries: the Cloud Run exchange's deadline, the snappy judge, the planner's coarsening and both snappy drivers' pass loops; the native toolchain is stubbed at the engine seam.
"""Job 470c3eb9 (shell-and-tube exchanger, internal snappy): pass 1 dispatched snappyHexMesh to
Cloud Run and no result came back in 50 minutes. The worker reported exit -3, "INFRASTRUCTURE
failure - the mesh run never started", the judge told the planner to resubmit the plan
unchanged, and pass 2 rebuilt the identical case for another 50 minutes. A dispatched run that
does not come back in time is a TIMEOUT, and it says so plainly.

Since D3 (docs/RULES.md TIME-1..3) a pass that runs past its guard is not re-planned coarser: the
guard is set from the pass's planned cells, a pass it stops ends the passes as a guard trip, and
the attempt is replayed as an infrastructure failure - so how coarse a mesh comes out never
depends on how busy the machine was. A pass that came out BROKEN (measured) is still followed by a
coarser one, and never rebuilt byte for byte."""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import meshpipeline.engines.snappy.drivers as drv
from meshpipeline.agents.builder.driver_run import BuilderDriverRun
from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE, RC_TIMED_OUT
from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo
from meshpipeline.engines.snappy.judge import _judge_snappy, _repair_message
from meshpipeline.engines.snappy.planner import coarsen_after_timeout

OP = "projects/p/locations/us-central1/operations/op-470c3eb9"


# the exchange: a dispatched run that never returns ran out of time

@pytest.fixture
def dispatched(monkeypatch):
    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc
    for k, v in {"GCP_PROJECT_ID": "proj", "GCP_MESH_BUCKET": "bkt", "CLOUDRUN_JOB": "job",
                 "GCP_REGION": "us-central1"}.items():
        monkeypatch.setattr(crc.provcfg, k, v)

    class _Blob:
        def upload_from_string(self, *_a, **_k):
            return None

        def exists(self):
            return False

    class _Client:
        def bucket(self, _name):
            return types.SimpleNamespace(blob=lambda *_a, **_k: _Blob())

    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.storage_client",
                        lambda: _Client())
    triggered: list = []

    def _trigger(**kw):
        triggered.append(kw)
        return crc.CloudRunOperationReference(OP)

    monkeypatch.setattr(crc, "_trigger_job", _trigger)
    # the result document never appears before the deadline (the remote ran 50m47s)
    monkeypatch.setattr(crc, "_poll_gcs_json", lambda *_a, **_k: None)
    return types.SimpleNamespace(crc=crc, triggered=triggered)


def test_a_dispatched_run_with_no_result_by_the_deadline_is_a_timeout(dispatched, tmp_path):
    r = dispatched.crc.run_mesh_remote(tmp_path, engine="snappy", timeout=2400,
                                       operation_key="a" * 64)
    assert dispatched.triggered, "the run was dispatched"
    assert r["timed_out"] is True
    assert r["rc"] == RC_TIMED_OUT and r["rc"] != RC_INFRASTRUCTURE
    assert "ran out of time" in r["log_tail"] and "50 min" in r["log_tail"]
    assert "never started" not in r["log_tail"]
    # the accepted operation travels back, so the claim records the run as accepted and a
    # replacement worker collects its late result rather than starting another
    assert r["provider_reference"] == OP


def test_the_judge_reads_it_as_a_timeout_and_asks_for_a_smaller_mesh(dispatched, tmp_path):
    r = dispatched.crc.run_mesh_remote(tmp_path, engine="snappy", timeout=2400,
                                       operation_key="b" * 64)
    ok, reason = _judge_snappy(r, {}, 0)
    assert not ok
    assert reason.startswith("TIMEOUT - snappyHexMesh ran out of time")
    assert "cells_across_diameter" in reason
    assert "never started" not in reason and "Resubmit this plan unchanged" not in reason


def test_a_stage_verdict_of_minus_3_is_a_run_that_ran_not_one_that_never_started():
    # parallel_stages reports -3 when every stage exited but the mesh is incomplete; it carries
    # the mesher's own note, never the dispatch marker
    r = {"rc": RC_INFRASTRUCTURE, "timed_out": False, "log_tail": "Finished meshing",
         "stage_note": "all stages exited zero but polyMesh is incomplete: missing owner"}
    msg = _repair_message("rc", r, {})
    assert "RAN" in msg and "polyMesh is incomplete" in msg
    assert "never started" not in msg and "Resubmit this plan unchanged" not in msg


# the planner's coarsening

_TIMED_OUT = {"approach": "Balanced internal snappyHexMesh, level-3 wall", "cells_across_diameter": 40,
              "surface_level": 3, "feature_level": 4, "n_layers": 5, "max_cells": 8_000_000}


def test_the_same_plan_after_a_timeout_is_cut_to_about_half_the_cells():
    out, cuts = coarsen_after_timeout(_TIMED_OUT, dict(_TIMED_OUT), ceiling=8_000_000,
                                      internal=True)
    assert out["cells_across_diameter"] == 28            # 0.7x: the wall cell grows ~1.4x
    assert out["max_cells"] == 4_000_000
    assert out["n_layers"] == 5, "the layers the user asked for are kept"
    assert cuts == ["cells across the bore cut from 40 to 28",
                    "cell budget cut from 8 M to 4 M"]


def test_lowering_only_the_surface_level_is_not_coarser():
    # the internal wall cell is bore / cells_across whatever the level - the re-plan the planner
    # guidance used to suggest would have timed out again
    proposal = {**_TIMED_OUT, "surface_level": 2, "feature_level": 3}
    out, cuts = coarsen_after_timeout(_TIMED_OUT, proposal, ceiling=8_000_000, internal=True)
    assert out["cells_across_diameter"] == 28 and out["surface_level"] == 2 and cuts


@pytest.mark.parametrize("proposal", [{**_TIMED_OUT, "cells_across_diameter": 30},
                                      {**_TIMED_OUT, "max_cells": 3_000_000}])
def test_a_re_plan_that_is_already_coarser_stands(proposal):
    out, cuts = coarsen_after_timeout(_TIMED_OUT, proposal, ceiling=8_000_000, internal=True)
    assert out == proposal and cuts == []


def test_nothing_left_to_cut_returns_the_plan_unchanged():
    floor = {"cells_across_diameter": 8, "max_cells": 200_000}
    out, cuts = coarsen_after_timeout(floor, dict(floor), ceiling=8_000_000, internal=True)
    assert out == floor and cuts == []


def test_an_external_plan_is_cut_by_its_budget():
    plan = {"max_cells": 6_000_000, "n_layers": 5}
    out, cuts = coarsen_after_timeout(plan, dict(plan), ceiling=8_000_000, internal=False)
    assert out["max_cells"] == 3_000_000 and "cells_across_diameter" not in out
    assert cuts == ["cell budget cut from 6 M to 3 M"]


def test_a_broken_pass_plan_is_remembered_for_the_next_attempt_only(tmp_path):
    a1, a2 = tmp_path / "attempt_1", tmp_path / "attempt_2"
    a1.mkdir()
    a2.mkdir()
    broken = {**_TIMED_OUT, "_why": "broken"}
    drv._write_timed_out_plan(a1, broken)
    assert drv._sibling_timed_out_plan(a2) == broken
    # an attempt never reads its own record: a re-executed node re-derives its passes as before
    assert drv._sibling_timed_out_plan(a1) is None
    # a record of a pass that ran out of TIME (written before D3) never makes a pass coarser
    drv._write_timed_out_plan(a1, _TIMED_OUT)
    assert drv._sibling_timed_out_plan(a2) is None


# the drivers, end to end, with the native toolchain stubbed at the engine seam

class _Publish:
    def __init__(self):
        self.notes: list[str] = []

    async def anote(self, text, *_a, **_k): self.notes.append(text)
    async def aerror(self, text, *_a, **_k): self.notes.append(text)
    async def awarn(self, *_a, **_k): pass
    async def astage(self, *_a, **_k): pass
    async def aattempt(self, *_a, **_k): pass
    async def acheck(self, *_a, **_k): pass
    async def arationale(self, *_a, **_k): pass
    async def atool_call(self, *_a, **_k): pass
    async def atool_result(self, *_a, **_k): pass
    async def ameshing(self, *_a, **_k): pass
    async def ameshed(self, *_a, **_k): pass
    async def aplan(self, *_a, **_k): pass
    async def amesh_stage(self, *_a, **_k): pass
    async def areasoning(self, *_a, **_k): pass


_TIMEOUT = {"rc": RC_TIMED_OUT, "timed_out": True, "provider_reference": OP,
            "log_tail": "[CLOUD_RUN_TIMEOUT] snappy: the mesh run ran out of time - no result "
                        "after 50 min."}
_CLEAN = {"cells": 1_500_000, "fatal": [], "skew_fraction": 0.0, "skew_faces": 0}


def _same_plan_every_time(monkeypatch, plan: dict, seen: list):
    async def _plan(**kw):
        seen.append(kw)
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"), input_tokens=1,
                              output_tokens=1)
        import meshpipeline.engines.snappy.planner as P
        return P.PlanOutcome(dict(plan), rr)

    import meshpipeline.engines.snappy.planner as P
    monkeypatch.setattr(P, "plan_with_accounting", _plan)


def _common(monkeypatch, attempts: int):
    monkeypatch.setattr(drv, "read_purpose", lambda *_a, **_k: "internal_cfd")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", attempts, raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})


def _run(ws: Path, state: dict, monkeypatch, source_path: str = ""):
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial", deadline_s=600.0)

    async def _fence(*_a, **_k):
        return None

    monkeypatch.setattr(run, "fence", _fence)
    pub = _Publish()
    out = asyncio.run(drv.drive(ws, state, job_id="j", publish=pub, run=run,
                                source_path=source_path))
    return out, pub


@pytest.fixture
def external(monkeypatch, tmp_path):
    import meshpipeline.engines.snappy.layer_policy as LP
    import meshpipeline.engines.snappy.snappy_runner as R
    w = types.SimpleNamespace(runs=[], fixed_case=False)

    def _render(workspace, *, strategy, **_k):
        ws = Path(workspace)
        (ws / "system").mkdir(parents=True, exist_ok=True)
        (ws / "system" / "blockMeshDict").write_text("box\n")
        budget = "fixed" if w.fixed_case else strategy.get("max_cells")
        (ws / "system" / "snappyHexMeshDict").write_text(f"maxGlobalCells {budget};\n")
        return {"surface_level": [3, 3], "n_layers": 5}

    w.guards = []

    def _run_native(*_a, **k):
        w.runs.append(len(w.runs) + 1)
        w.guards.append(k.get("timeout"))
        return dict(_TIMEOUT) if len(w.runs) == 1 else {"rc": 0, "timed_out": False}

    stubs = {
        "detect_symmetry_plane": lambda *_a, **_k: None,
        "domain_from_strategy": lambda *_a, **_k: ([0, 0, 0], [1, 1, 1]),
        "prepare_surface": lambda *_a, **_k: {"surface_name": "body",
                                              "feature_file": "body.eMesh"},
        "render_snappy_case": _render,
        "run_snappy": _run_native,
        "check_mesh": lambda *_a, **_k: dict(_CLEAN),
        "_patch_face_counts": lambda *_a, **_k: {"body": 52675, "farfield": 100},
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(R, name, fn, raising=False)
    monkeypatch.setattr(LP, "measure_field", lambda *_a, **_k: None)
    monkeypatch.setattr(LP, "write_layer_policy", lambda *_a, **_k: None)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda *_a, **_k: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 1.0,
                                           "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda *_a, **_k: {"surface_level": [3, 3], "feature_level": 4,
                                           "afford_level": 3})
    monkeypatch.setattr("meshpipeline.engines.workspace_facts.contract_wall_patch",
                        lambda *_a, **_k: "body")
    w.plans = []
    _same_plan_every_time(monkeypatch, {"approach": "same plan", "max_cells": 6_000_000,
                                        "n_layers": 5}, w.plans)
    _common(monkeypatch, attempts=3)

    def go():
        from tests._geometry_support import geometry_state as _geometry_state

        from meshpipeline.contracts.geometry_units import LengthUnit
        ws = tmp_path / "attempt_1"
        ws.mkdir()
        (ws / "input.stl").write_text("solid x\nendsolid x\n")
        state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
                 "intake_patches": [], "dimensionality": "3D", "flow_topology": "external",
                 "geometry": _geometry_state(tmp_path / "_src", unit=LengthUnit.metre,
                                             filename="body.stl")}
        return (ws,) + _run(ws, state, monkeypatch)
    w.go = go
    return w


def _guard_of(cells: int) -> int:
    from meshpipeline.contracts.work_limits import guard_for
    return guard_for("snappy", cells=cells).guard_seconds


def test_a_pass_its_guard_stops_ends_the_passes_as_a_guard_trip(external):
    from meshpipeline.engines import determinism as det
    ws, (ok, _value, _outcome), pub = external.go()
    # no coarser pass 2: the attempt ends on the trip and is replayed as it was (TIME-3)
    assert ok is False and external.runs == [1]
    assert not any(n.startswith("Meshing pass 2") for n in pub.notes), pub.notes
    outcome = [n for n in pub.notes if n.startswith("Pass 1 ")]
    assert outcome and outcome[0].startswith("Pass 1 stopped by its guard clock"), outcome
    assert "infrastructure stop" in outcome[0]
    assert not (ws / ".timed_out_plan.json").exists(), "a clock never makes a later pass coarser"
    # the guard came from the pass's planned cells (TIME-2), and the trip is beside the case
    assert external.guards == [_guard_of(6_000_000)]
    trips = det.guard_trips(ws)
    assert [(t["where"], t["step"], t["amount"]) for t in trips] == [("snappy", "pass 1",
                                                                       6_000_000.0)]


def test_a_pass_guard_comes_from_its_planned_cells_alone():
    # same planned cells in, same guard out - the elapsed time is not an input (TIME-2)
    assert _guard_of(6_000_000) == 4800                              # the snappy ceiling
    assert _guard_of(2_000_000) == 4000                              # 3 x 2 M / 1,500 cells/s
    assert _guard_of(500_000) == 2400                                # never under the old cap
    work = drv.pass_guard(2_000_000, job_id="", attempt=1)
    assert work.as_dict()["planned_s"] == pytest.approx(2_000_000 / 1500.0, abs=0.1)


def test_a_pass_broken_where_its_layers_were_not_the_cause_is_followed_by_a_coarser_one(
        external, monkeypatch):
    # the enodise splitter plate: the same plan rebuilt the same broken mesh on four passes
    # (verify-5fix). Once the fold is known not to be the layers', the next pass is coarser.
    import meshpipeline.engines.snappy.drivers as drv
    import meshpipeline.engines.snappy.snappy_runner as R
    meshes = iter([{"cells": 2_741_541, "fatal": ["incorrectly oriented faces"],
                    "skew_faces": 705, "skew_fraction": 1e-4}, dict(_CLEAN)])
    monkeypatch.setattr(R, "check_mesh", lambda *_a, **_k: next(meshes))
    monkeypatch.setattr(R, "run_snappy", lambda *_a, **_k: (external.runs.append(1),
                                                            {"rc": 0, "timed_out": False})[1])

    def _not_the_layers(*_a, not_layers=None, **_k):
        if not_layers is not None:
            not_layers.append(True)
        return None
    monkeypatch.setattr(drv, "_repair_after_fold", _not_the_layers)
    ws, (ok, _value, _outcome), pub = external.go()
    assert ok is True and len(external.runs) == 2
    opened = [n for n in pub.notes if n.startswith("Meshing pass 2 of")]
    assert opened and ("made coarser because the last pass came out broken where its layers "
                       "were not the cause") in opened[0], opened
    assert "cell budget cut from 6 M to 3 M" in opened[0]
    rec = json.loads((ws / ".timed_out_plan.json").read_text())
    assert rec["max_cells"] == 6_000_000 and rec["_why"] == "broken"


@pytest.fixture
def internal(monkeypatch, tmp_path):
    """The exchanger's shape of failure on the internal driver: a box-shaped passage, the planner
    returning the timed-out plan every time, the first run out of time."""
    import meshpipeline.engines.snappy.snappy_runner as R
    from meshpipeline.cad.stl_io import _box_triangles, _write_solid
    w = types.SimpleNamespace(runs=[], cases=[])
    stl_dir = tmp_path / "attempt_1" / "_internal_stls"

    def _stl(name, lo, hi):
        stl_dir.mkdir(parents=True, exist_ok=True)
        p = stl_dir / f"{name}.stl"
        with p.open("w") as fh:
            _write_solid(fh, name, _box_triangles(lo, hi))
        return str(p)

    def _tess(*_a, **_k):
        return {"stls": {"wall": _stl("wall", [0, 0, 0], [0.5, 0.3, 0.3]),
                         "inlet": _stl("inlet", [0, 0.1, 0.1], [0.001, 0.2, 0.2]),
                         "outlet": _stl("outlet", [0.499, 0.1, 0.1], [0.5, 0.2, 0.2])},
                "interior_point": [0.25, 0.15, 0.15], "bbox_min": [0, 0, 0],
                "bbox_max": [0.5, 0.3, 0.3],
                "openings": {"inlet": {"area": 0.00683, "centroid": [0.0, 0.15, 0.15]},
                             "outlet": {"area": 0.00683, "centroid": [0.5, 0.15, 0.15]}}}

    w.timeouts = 1                    # how many runs, from the first, run out of time

    def _run_native(workspace, **_k):
        w.runs.append(len(w.runs) + 1)
        w.cases.append((Path(workspace) / "system" / "snappyHexMeshDict").read_text())
        return (dict(_TIMEOUT) if len(w.runs) <= w.timeouts
                else {"rc": 0, "timed_out": False})

    monkeypatch.setattr(R, "tessellate_internal", _tess)
    monkeypatch.setattr(R, "run_snappy", _run_native)
    monkeypatch.setattr(R, "check_mesh", lambda *_a, **_k: dict(_CLEAN))
    monkeypatch.setattr(R, "_patch_face_counts",
                        lambda *_a, **_k: {"wall": 810_216, "inlet": 900, "outlet": 900})
    monkeypatch.setattr(drv, "_plan_surface", lambda *_a, **_k: types.SimpleNamespace(
        consumed=None))
    w.plans = []
    _same_plan_every_time(monkeypatch, dict(_TIMED_OUT), w.plans)
    _common(monkeypatch, attempts=3)

    def go():
        from tests._geometry_support import geometry_state as _geometry_state

        from meshpipeline.contracts.geometry_units import LengthUnit
        ws = tmp_path / "attempt_1"
        ws.mkdir(exist_ok=True)
        src = tmp_path / "part.step"
        src.write_text("ISO-10303-21;\n")
        state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
                 "intake_patches": [], "dimensionality": "3D", "flow_topology": "internal",
                 "input_kind": "fluid-domain", "purpose": "internal_cfd",
                 "geometry": _geometry_state(tmp_path / "_src", unit=LengthUnit.metre,
                                             filename="part.step")}
        return (ws,) + _run(ws, state, monkeypatch, source_path=str(src))
    w.go = go
    return w


def test_an_internal_pass_its_guard_stops_is_a_guard_trip_not_a_coarser_re_plan(internal):
    from meshpipeline.engines import determinism as det
    ws, (ok, _value, _outcome), pub = internal.go()
    # the exchanger's pass 1 runs past its guard: before D3 pass 2 was re-planned at 28 cells
    # across the bore. Now the passes stop and the attempt is replayed as it was (TIME-3).
    assert ok is False and internal.runs == [1]
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    assert len(filling) == 1 and "about 40 cells across the bore" in filling[0]
    outcome = [n for n in pub.notes if n.startswith("Pass 1 ")]
    assert outcome[0].startswith("Pass 1 stopped by its guard clock")
    assert not any("never started" in n or "no cells, 0 faces" in n for n in pub.notes)
    assert json.loads((ws / ".last_plan.json").read_text())["cells_across_diameter"] == 40
    assert [t["where"] for t in det.guard_trips(ws)] == ["snappy"]


def test_an_internal_run_inside_its_guard_meshes_on_the_first_pass(internal):
    internal.timeouts = 0
    _ws, (ok, _value, _outcome), pub = internal.go()
    assert ok is True and internal.runs == [1]
    assert not any("guard clock" in n for n in pub.notes)


@pytest.mark.parametrize("kept, says", [
    (True, "refining locally as far as the cell budget allows (3 of 5 thin spots left"),
    (False, "not refined locally (3 of 5 thin spots left"),
])
def test_the_thin_feature_note_never_promises_what_the_budget_cut(internal, monkeypatch, kept,
                                                                   says):
    from meshpipeline.cad import thin_features as TF

    def _boxes(*_a, **_k):
        box = {"min": [0.2, 0.1, 0.1], "max": [0.21, 0.2, 0.2], "level_bump": 1,
               "thinnest_m": 0.003, "n_triangles": 12, "volume_m3": 1e-5, "est_cells": 100}
        return TF.ThinRegions([box] if kept else [], thinnest_m=0.003, found=5,
                              note="3 of 5 thin spots left unrefined: they would need about "
                                   "2.0 M cells, over the 0.5 M cells allowed for local "
                                   "refinement")

    monkeypatch.setattr(TF, "thin_refinement_boxes", _boxes)
    internal.timeouts = 0
    _ws, (ok, _value, _outcome), pub = internal.go()
    assert ok is True
    thin = [n for n in pub.notes if n.startswith("Thin feature detected")]
    assert thin and thin[0].startswith("Thin feature detected - 3.0 mm across"), thin
    assert says in thin[0]
    assert "so it is captured" not in thin[0]
