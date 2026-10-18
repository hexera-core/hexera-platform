# Responsibility: Verify a review's retry that authors the rejected case again stops before the mesher, and nothing else does.
# Boundaries: the snappy driver and its seam to the builder; the graph's end of it is tested beside the graph.
"""Job e0fa8ad0: the review rejected the half-CRM, the re-plan changed nothing the mesher reads, and
attempts 2 and 3 each spent ~10 minutes rebuilding the identical 1,219,085-cell mesh for the review
to reject again. A retry that writes the case the review rejected, byte for byte, now stops before
the mesher runs; any difference at all - or any doubt - meshes as before."""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

import meshpipeline.engines.snappy.drivers as drv
from meshpipeline.agents.builder.driver_run import STOP_REVIEWED_CASE_REPEATS, BuilderDriverRun
from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo

_DICTS = {"system/blockMeshDict": "box 1 2 3\n",
          "system/snappyHexMeshDict": "layers { aircraft { nSurfaceLayers 5; } }\n",
          "layer_policy.json": '{"mode": "uniform"}'}


def _case(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _review_retry_state(reviewed: Path, **over) -> dict:
    st = {"classifier_result": {"error_source": "reviewer_fail", "summary": "add a wake region"},
          "openfoam_workspace": str(reviewed)}
    st.update(over)
    return st


# which retries are checked

def test_only_a_retry_the_review_caused_is_checked(tmp_path):
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    assert drv._reviewed_workspace(_review_retry_state(reviewed)) == reviewed
    for source in ("executor_fail", "requirements_near_miss", "engine_fallback", ""):
        st = _review_retry_state(reviewed, classifier_result={"error_source": source})
        assert drv._reviewed_workspace(st) is None, f"{source!r} retry was checked"
    assert drv._reviewed_workspace({}) is None


def test_a_dispute_rebuild_is_never_stopped(tmp_path):
    reviewed = _case(tmp_path / "attempt_1", _DICTS)
    st = _review_retry_state(reviewed, user_dispute={"comment": "refine the wake"})
    assert drv._reviewed_workspace(st) is None


def test_a_reviewed_workspace_that_is_gone_is_not_a_reference(tmp_path):
    st = _review_retry_state(tmp_path / "attempt_9")
    assert drv._reviewed_workspace(st) is None


# what counts as the same case

def test_the_identical_case_repeats(tmp_path):
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    mine = _case(tmp_path / "attempt_3", _DICTS)
    assert drv._repeats_reviewed_case(mine, reviewed) is True


@pytest.mark.parametrize("rel", sorted(_DICTS))
def test_one_changed_byte_in_any_case_file_is_a_new_case(tmp_path, rel):
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    mine = _case(tmp_path / "attempt_3", {**_DICTS, rel: _DICTS[rel].replace("1", "7", 1)
                                          if "1" in _DICTS[rel] else _DICTS[rel] + " "})
    assert drv._repeats_reviewed_case(mine, reviewed) is False


def test_a_policy_on_one_side_only_is_a_new_case(tmp_path):
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    no_policy = {k: v for k, v in _DICTS.items() if k != "layer_policy.json"}
    mine = _case(tmp_path / "attempt_3", no_policy)
    assert drv._repeats_reviewed_case(mine, reviewed) is False
    # and with no policy on EITHER side, the two dictionaries decide
    assert drv._repeats_reviewed_case(mine, _case(tmp_path / "attempt_x", no_policy)) is True


def test_doubt_never_stops_a_mesh(tmp_path):
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    assert drv._repeats_reviewed_case(tmp_path / "empty", reviewed) is False
    assert drv._repeats_reviewed_case(_case(tmp_path / "a3", _DICTS), None) is False
    # an absent case is never "the same case", even against another absent one
    (tmp_path / "e1").mkdir()
    (tmp_path / "e2").mkdir()
    assert drv._repeats_reviewed_case(tmp_path / "e1", tmp_path / "e2") is False
    # the reviewed workspace itself is not a retry of itself
    assert drv._repeats_reviewed_case(reviewed, reviewed) is False


# the driver, end to end, with the native toolchain stubbed at the engine seam

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
    async def areasoning(self, *_a, **_k): pass


@pytest.fixture
def stubbed(monkeypatch):
    ops: list[str] = []
    case = {"files": dict(_DICTS)}
    import meshpipeline.engines.snappy.snappy_runner as _sr

    def _render(workspace, **_k):
        ops.append("render")
        _case(Path(workspace), {k: v for k, v in case["files"].items()
                                if k != "layer_policy.json"})
        return {"surface_level": [5, 5], "n_layers": 5}

    def _write_policy(workspace, _policy):
        if "layer_policy.json" in case["files"]:
            _case(Path(workspace), {"layer_policy.json": case["files"]["layer_policy.json"]})

    stubs = {
        "detect_symmetry_plane": lambda *_a, **_k: None,
        "domain_from_strategy": lambda *_a, **_k: ([0, 0, 0], [1, 1, 1]),
        "prepare_surface": lambda *_a, **_k: {"surface_name": "aircraft",
                                              "feature_file": "aircraft.eMesh"},
        "render_snappy_case": _render,
        "run_snappy": lambda *_a, **_k: ops.append("run_native") or {"rc": 0},
        "check_mesh": lambda *_a, **_k: {"cells": 1219085, "fatal": [], "skew_fraction": 0.0,
                                         "skew_faces": 0},
        "_patch_face_counts": lambda *_a, **_k: {"aircraft": 52675, "farfield": 100},
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(_sr, name, fn, raising=False)
    import meshpipeline.engines.snappy.layer_policy as LP
    monkeypatch.setattr(LP, "measure_field", lambda *_a, **_k: None)
    monkeypatch.setattr(LP, "write_layer_policy", _write_policy)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda *_a, **_k: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 1.0,
                                           "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda *_a, **_k: {"surface_level": [5, 5], "feature_level": 7,
                                           "afford_level": 5})
    monkeypatch.setattr("meshpipeline.engines.workspace_facts.contract_wall_patch",
                        lambda *_a, **_k: "aircraft")
    monkeypatch.setattr(drv, "read_purpose", lambda *_a, **_k: "external")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})

    async def _plan(**_kw):
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"), input_tokens=1,
                              output_tokens=1)
        import meshpipeline.engines.snappy.planner as P
        return P.PlanOutcome({"approach": "same plan", "max_cells": 1_200_000, "n_layers": 5}, rr)

    import meshpipeline.engines.snappy.planner as P
    monkeypatch.setattr(P, "plan_with_accounting", _plan)
    monkeypatch.setattr("meshpipeline.engines.snappy.planner.clamp_cell_budget",
                        lambda v, ceiling=None: v or 1_200_000)
    return types.SimpleNamespace(ops=ops, case=case)


def _drive(tmp_path: Path, monkeypatch, state_extra: dict):
    from tests._geometry_support import geometry_state as _geometry_state

    from meshpipeline.contracts.geometry_units import LengthUnit
    ws = tmp_path / "attempt_3"
    ws.mkdir()
    (ws / "input.stl").write_text("solid x\nendsolid x\n")
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="retry", deadline_s=60.0)

    async def _fence(*_a, **_k):
        return None

    monkeypatch.setattr(run, "fence", _fence)
    state = {"builder_mode": "retry", "engine": "snappy", "request_txt": "r",
             "intake_patches": [], "dimensionality": "3D", "flow_topology": "external",
             "retry_count": 2,
             "geometry": _geometry_state(tmp_path / "_src", unit=LengthUnit.metre,
                                         filename="crm.stl"),
             **state_extra}
    pub = _Publish()
    out = asyncio.run(drv.drive(ws, state, job_id="j", publish=pub, run=run))
    return out, pub


def test_a_review_retry_that_writes_the_rejected_case_never_reaches_the_mesher(
        stubbed, tmp_path, monkeypatch):
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    (ok, value, outcome), pub = _drive(tmp_path, monkeypatch, _review_retry_state(reviewed))
    assert ok is False and value == STOP_REVIEWED_CASE_REPEATS
    assert outcome.failure_marker == STOP_REVIEWED_CASE_REPEATS
    assert "render" in stubbed.ops and "run_native" not in stubbed.ops
    # the driver announces no carving and no mesh it never started (the builder node tells the
    # user why the attempt stopped - one closing note per attempt)
    assert not any(n.startswith("Carving the body") for n in pub.notes)


def test_a_review_retry_that_changes_the_case_meshes_as_before(stubbed, tmp_path, monkeypatch):
    reviewed = _case(tmp_path / "attempt_2", {**_DICTS, "system/snappyHexMeshDict": "older\n"})
    (ok, value, _outcome), pub = _drive(tmp_path, monkeypatch, _review_retry_state(reviewed))
    assert ok is True and value != STOP_REVIEWED_CASE_REPEATS
    assert "run_native" in stubbed.ops
    assert not any(n.startswith("Stopping:") for n in pub.notes)


def test_a_gate_retry_with_the_same_case_still_meshes(stubbed, tmp_path, monkeypatch):
    # a gate failure has its own no-progress stop (agents/builder/no_progress.py); this one is
    # only for reviews, so the same case after a gate failure meshes exactly as it always did
    reviewed = _case(tmp_path / "attempt_2", _DICTS)
    st = _review_retry_state(reviewed, classifier_result={"error_source": "executor_fail"})
    (ok, _value, _outcome), _pub = _drive(tmp_path, monkeypatch, st)
    assert ok is True and "run_native" in stubbed.ops
