# Responsibility: Verify a doomed run is stopped before the mesher starts, and reported as what stopped it.
# Boundaries: the domain pre-flight, the zero-face split, the refusal record, the snappy driver's re-plan, and the executor's report.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines.preflight import (
    PREFLIGHT_RECORD,
    PreflightRefusal,
    check_domain,
    read_refusal,
    zero_face_feedback,
)
from meshpipeline.engines.snappy import snappy_runner as R

#: what recommend_refinement hands the renderer, cut to the keys it reads
REC = {"base_cell": 0.1, "surface_level": [2, 3], "afford_level": 3, "feature_level": 3,
       "distance_bands": [(0.05, 3), (0.3, 2)], "resolve_feature_angle": 30}
CAR = [("car", "wall"), ("farfield", "farfield")]


def _patches(pairs) -> list[dict]:
    return [{"name": n, "type": t} for n, t in pairs]


# the domain check

class TestTheDomainBeforeMeshing:
    BODY = ([0.0, 0.0, 0.0], [1.0, 0.4, 0.3])

    def _check(self, dmin, dmax, *, strict=False, grounded=False):
        return check_domain(requested={"upstream": 2.0, "downstream": 5.0},
                            reference_length_m=1.0, strict=strict, flow_axis="+x",
                            body_min=self.BODY[0], body_max=self.BODY[1],
                            domain_min=dmin, domain_max=dmax, grounded=grounded)

    def test_a_box_the_gate_would_block_is_refused_with_its_numbers(self):
        r = self._check([-2.0, -2.0, -2.0], [2.0, 2.0, 2.0])      # 1 L downstream of 5 asked
        assert r is not None and r.gate == "domain_extent"
        assert r.cause == FailureCause.DOMAIN_EXTENT
        assert r.facts["before_meshing"] is True
        assert {"direction": "downstream", "requested": 5.0, "measured": 1.0} in r.facts["misses"]

    def test_a_right_sized_box_passes(self):
        assert self._check([-2.0, -2.0, -2.0], [6.0, 2.0, 2.0]) is None

    def test_a_near_miss_is_meshed_when_lenient_and_refused_when_strict(self):
        near = ([-2.0, -2.0, -2.0], [4.6, 2.0, 2.0])               # 3.6 L of 5: a near miss
        assert self._check(*near) is None
        assert self._check(*near, strict=True) is not None

    def test_a_grounded_body_is_judged_on_the_room_above(self):
        # the floor sits on the body by design; only the room above counts as vertical
        assert check_domain(requested={"vertical": 2.0}, reference_length_m=1.0, strict=True,
                            flow_axis="+x", body_min=[0.0, 0.0, 0.0], body_max=[1.0, 0.4, 0.3],
                            domain_min=[-2.0, -2.0, 0.0], domain_max=[6.0, 2.4, 2.5],
                            grounded=True) is None

    def test_no_request_or_no_ruler_is_never_refused(self):
        assert check_domain(requested=None, reference_length_m=1.0, strict=True, flow_axis=None,
                            body_min=[0] * 3, body_max=[1] * 3, domain_min=[0] * 3,
                            domain_max=[1] * 3, grounded=False) is None
        assert check_domain(requested={"upstream": 5.0}, reference_length_m=None, strict=True,
                            flow_axis=None, body_min=[0] * 3, body_max=[1] * 3,
                            domain_min=[0] * 3, domain_max=[1] * 3, grounded=False) is None


class TestTheZeroFaceGateTellsItsTwoCausesApart:
    def _boundary(self, ws: Path, counts: dict) -> None:
        pm = ws / "constant" / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        body = "".join(f"    {n}\n    {{\n        type wall;\n        nFaces {c};\n"
                       f"        startFace 0;\n    }}\n" for n, c in counts.items())
        (pm / "boundary").write_text(f"FoamFile {{}}\n{len(counts)}\n(\n{body})\n")

    def test_a_patch_not_in_the_mesh_is_a_contract_mismatch(self, tmp_path):
        # job ac1daa3e: 'car wall' approved, 'car_wall' written, zero faces on both attempts
        self._boundary(tmp_path, {"car_wall": 5000, "ground": 900, "farfield": 300})
        fb = zero_face_feedback("zero faces: ['car wall']", ["car wall"], tmp_path)
        assert fb.cause == FailureCause.CONTRACT_MISMATCH
        assert fb.facts["renamed"] == {"car wall": "car_wall"}
        assert fb.facts["present"] == ["car_wall", "farfield", "ground"]

    def test_a_patch_in_the_mesh_with_no_faces_was_lost_by_the_mesher(self, tmp_path):
        self._boundary(tmp_path, {"wall": 5000, "inlet": 200, "outlet_2": 0})
        fb = zero_face_feedback("zero faces: ['outlet_2']", ["outlet_2"], tmp_path)
        assert fb.cause == FailureCause.PATCH_NOT_CAPTURED
        assert fb.facts["patches"] == ["outlet_2"]

    def test_a_dropped_patch_with_no_other_spelling_keeps_its_retries(self, tmp_path):
        # cfMesh and createPatch can drop an empty patch from the boundary altogether: an
        # absence alone proves nothing about naming, so the retry-ending cause is not claimed
        self._boundary(tmp_path, {"wall": 5000, "inlet": 200})
        fb = zero_face_feedback("zero faces: ['outlet']", ["outlet"], tmp_path)
        assert fb.cause == FailureCause.PATCH_NOT_CAPTURED
        assert fb.facts["patches"] == ["outlet"]

    def test_a_renamed_patch_beside_a_lost_one_is_still_the_mismatch(self, tmp_path):
        self._boundary(tmp_path, {"car_wall": 5000, "farfield": 300})
        fb = zero_face_feedback("zero faces", ["car wall", "outlet"], tmp_path)
        assert fb.cause == FailureCause.CONTRACT_MISMATCH
        assert fb.facts["missing"] == ["car wall"]

    def test_no_boundary_to_read_is_an_incomplete_mesh_not_a_naming_verdict(self, tmp_path):
        fb = zero_face_feedback("zero faces: ['inlet']", ["inlet"], tmp_path)
        assert fb.cause == FailureCause.ENGINE_CRASHED


def test_the_record_round_trips(tmp_path):
    PreflightRefusal(gate="patch_contract", cause=FailureCause.CONTRACT_MISMATCH,
                     builder_text="x", facts={"missing": ["a"]}).write(tmp_path)
    r = read_refusal(tmp_path)
    assert r is not None and r.gate == "patch_contract" and r.facts == {"missing": ["a"]}
    (tmp_path / PREFLIGHT_RECORD).write_text("not json")
    assert read_refusal(tmp_path) is None


# the snappy driver re-plans a doomed box instead of meshing it

class _Publish:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name, a, k):
        self.calls.append((name, a, k))

    async def anote(self, *a, **k): self._rec("anote", a, k)
    async def awarn(self, *a, **k): self._rec("awarn", a, k)
    async def aerror(self, *a, **k): self._rec("aerror", a, k)
    async def astage(self, *a, **k): self._rec("astage", a, k)
    async def aattempt(self, *a, **k): self._rec("aattempt", a, k)
    async def acheck(self, *a, **k): self._rec("acheck", a, k)
    async def aaction(self, *a, **k): self._rec("aaction", a, k)
    async def arationale(self, *a, **k): self._rec("arationale", a, k)
    async def areasoning(self, *a, **k): self._rec("areasoning", a, k)
    async def atool_call(self, *a, **k): self._rec("atool_call", a, k)
    async def atool_result(self, *a, **k): self._rec("atool_result", a, k)
    async def ameshing(self, *a, **k): self._rec("ameshing", a, k)
    async def ameshed(self, *a, **k): self._rec("ameshed", a, k)
    async def averdict(self, *a, **k): self._rec("averdict", a, k)
    async def aclosing(self, *a, **k): self._rec("aclosing", a, k)


@pytest.fixture
def driver(monkeypatch, tmp_path):
    """The real snappy driver and case renderer; only the planner, the geometry measurement and
    the native mesher are stood in for."""
    from tests._geometry_support import geometry_state

    import meshpipeline.engines.snappy.drivers as drv
    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo

    seen: dict = {"native": 0, "plans": 0, "boxes": []}
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")
    real_render = R.render_snappy_case

    def _prepare(ws, **k):
        (Path(ws) / "system").mkdir(parents=True, exist_ok=True)
        return {"surface_name": k["wall_patch"], "feature_file": f"{k['wall_patch']}.eMesh"}

    def _domain(*a, **k):
        box = seen["boxes"].pop(0) if seen["boxes"] else ([-5.0, -5.2, -5.0], [11.0, 5.2, 5.3])
        return list(box[0]), list(box[1])

    def _native(*a, **k):
        seen["native"] += 1
        return {"rc": 0}

    for name, fn in {
        "detect_symmetry_plane": lambda *a, **k: None,
        "domain_from_strategy": _domain,
        "prepare_surface": _prepare,
        "render_snappy_case": lambda ws, **k: real_render(ws, **{**k, "recommendation": REC}),
        "run_snappy": _native,
        "check_mesh": lambda *a, **k: {"cells": 1000, "fatal": [], "skew_fraction": 0.0,
                                       "skew_faces": 0},
        "_patch_face_counts": lambda *a, **k: {"car": 500, "farfield": 300},
    }.items():
        monkeypatch.setattr(R, name, fn, raising=False)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda *a, **k: {"diag": 1.0, "extent": [1.0, 0.4, 0.3],
                                         "bbox_min": [0.0, -0.2, 0.0], "bbox_max": [1.0, 0.2, 0.3],
                                         "surface_area": 1.0, "min_feature": 0.01, "L": 1.0})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement", lambda *a, **k: REC)
    monkeypatch.setattr("meshpipeline.engines.workspace_facts.contract_wall_patch",
                        lambda *a, **k: "car")
    monkeypatch.setattr(drv, "read_purpose", lambda *a, **k: "external_cfd")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded", lambda **kw: {})

    async def _plan(**_kw):
        seen["plans"] += 1
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"),
                              input_tokens=1, output_tokens=1)
        return planner.PlanOutcome({"approach": "a", "max_cells": 100000}, rr)

    monkeypatch.setattr(planner, "plan_with_accounting", _plan)
    monkeypatch.setattr(planner, "clamp_cell_budget", lambda v, ceiling=None: v or 100000)

    def run(passes=1, **state_extra):
        monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", passes, raising=False)
        builder_run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial",
                                       deadline_s=60.0)

        async def _fence(*_a, **_k):
            return None
        monkeypatch.setattr(builder_run, "fence", _fence)
        pub = _Publish()
        state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
                 "intake_patches": _patches(CAR), "dimensionality": "3D",
                 "flow_topology": "external", "flow_axis": "+x",
                 "geometry": geometry_state(Path(tmp_path) / "_src", unit=LengthUnit.metre,
                                            filename="body.stl"), **state_extra}
        ok, _value, _outcome = asyncio.run(drv.drive(Path(tmp_path), state, job_id="j",
                                                     publish=pub, run=builder_run))
        return ok, pub

    return run, seen, tmp_path


SHORT_BOX = ([-0.5, -5.0, -5.0], [1.5, 5.0, 5.0])         # 0.5 L downstream: under half of 5
GOOD_BOX = ([-3.0, -5.0, -5.0], [7.0, 5.0, 5.0])
ASKED = {"requested_extents": {"upstream": 2.0, "downstream": 5.0}, "reference_length_m": 1.0}


class TestTheDriverStopsBeforeTheMesher:
    def test_a_box_that_would_fail_the_extent_gate_is_replanned_not_meshed(self, driver):
        run, seen, ws = driver
        seen["boxes"] = [SHORT_BOX, GOOD_BOX]
        ok, pub = run(passes=2, **ASKED)
        assert ok is True and seen["native"] == 1, "only the right-sized box is meshed"
        assert seen["plans"] == 2, "the refused box was re-planned"
        notes = [a[0] for name, a, _k in pub.calls if name == "anote" and a]
        assert any("not meshed" in n and "re-planning" in n for n in notes), notes
        assert not (ws / PREFLIGHT_RECORD).exists()

    def test_every_pass_refused_never_starts_the_mesher_and_says_why(self, driver):
        run, seen, ws = driver
        seen["boxes"] = [SHORT_BOX, SHORT_BOX]
        ok, pub = run(passes=2, **ASKED)
        assert ok is False and seen["native"] == 0
        rec = read_refusal(ws)
        assert rec is not None and rec.gate == "domain_extent" and rec.cause == "domain_extent"
        errors = [(a, k) for name, a, k in pub.calls if name == "aerror"]
        assert errors and errors[0][1]["op_id"] == "snappy:preflight-refused:domain_extent"
        assert "downstream is 0.5 reference lengths" in errors[0][0][0]

    def test_without_a_stated_request_the_box_is_never_judged_early(self, driver):
        run, seen, _ws = driver
        seen["boxes"] = [SHORT_BOX]
        ok, _pub = run()
        assert ok is True and seen["native"] == 1


# the executor reports a recorded refusal instead of a mesher that "did not finish"

@pytest.fixture
def quiet_executor(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.pipeline.executor as ex

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    # the gate sequence is under test, not Redis or PostgreSQL: the port is stood in for
    install(monkeypatch, _ep)
    monkeypatch.setattr(ex, "execution_publisher", _ep.execution_publisher)
    return ex


@pytest.mark.parametrize(("gate", "cause", "facts"), [
    ("domain_extent", "domain_extent",
     {"misses": [{"direction": "downstream", "requested": 5.0, "measured": 0.5}],
      "before_meshing": True}),
    # the shape a patch-contract launch check records (engines/case_contract.py on
    # fix/approved-equals-delivered is meant to write this record)
    ("patch_contract", "contract_mismatch",
     {"missing": ["car wall"], "renamed": {"car wall": "car_wall"}, "before_meshing": True}),
])
def test_the_executor_reports_the_refusal_as_its_gate_and_cause(quiet_executor, monkeypatch,
                                                                tmp_path, gate, cause, facts):
    ex = quiet_executor

    class _NeverFinalize:
        name = "snappy"

        def finalize(self, *a, **k):
            raise AssertionError("an empty workspace must not be finalized")
    monkeypatch.setattr(ex, "get_engine", lambda n="": _NeverFinalize())
    PreflightRefusal(gate=gate, cause=cause, builder_text="[PREFLIGHT] refused",
                     facts=facts).write(tmp_path)
    out = asyncio.run(ex.node_executor({"openfoam_workspace": str(tmp_path), "job_id": "t",
                                        "engine": "snappy", "intake_patches": _patches(CAR)}))
    assert out["executor_success"] is False
    assert out["executor_failed_gate"] == gate
    assert out["executor_failure_cause"] == cause
    assert out["executor_failure_facts"] == facts
    assert "[PREFLIGHT] refused" in out["executor_output"]


def test_a_mesh_on_disk_outranks_a_stale_refusal(quiet_executor, monkeypatch, tmp_path):
    ex = quiet_executor
    calls = []

    class _Finalize:
        name = "snappy"

        def finalize(self, *a, **k):
            calls.append(1)
            return {"success": False, "output": "[SNAPPY] incomplete"}
    monkeypatch.setattr(ex, "get_engine", lambda n="": _Finalize())
    PreflightRefusal(gate="domain_extent", cause="domain_extent", builder_text="x",
                     facts={}).write(tmp_path)
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "owner").write_text("x")
    out = asyncio.run(ex.node_executor({"openfoam_workspace": str(tmp_path), "job_id": "t",
                                        "engine": "snappy", "intake_patches": []}))
    assert calls == [1]
    assert out["executor_failed_gate"] == "finalize"
    assert out["executor_failure_cause"] == "engine_crashed"
