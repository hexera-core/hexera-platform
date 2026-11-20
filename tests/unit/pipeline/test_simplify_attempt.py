# Responsibility: Verify the run simplifies its own geometry only behind CAD_SIMPLIFY_ENABLED and on inspection's evidence, and that every way the attempt can end short leaves the run on the geometry it had.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_simplify as scfg
from meshpipeline.cad.simplify.contracts import FeatureCandidate, FeatureKind, SimplifyReport
from meshpipeline.cad.simplify.simplify import SimplifyRefused, SimplifyUnavailable
from meshpipeline.pipeline.simplify_attempt import node_simplify_attempt

# THE INVARIANT EVERY TEST HERE CHECKS, as for the repair attempt: the worst outcome is the
# behaviour of not having tried - the run carries on with the geometry it had, and says why.


def _candidate(fid="hole0"):
    return FeatureCandidate(id=fid, kind=FeatureKind.hole, faces=(7,), radius=2.0, radius_ratio=0.01,
                            concave=True, area=125.0, centroid=(100.0, 30.0, 5.0),
                            bbox=(98.0, 28.0, 0.0, 102.0, 32.0, 10.0), feature_size=0.5,
                            est_surface_cells=200)


def _state(tmp_path, **over) -> dict:
    state = {"job_id": "job-1", "engine": "gmsh", "geometry": geometry_state(tmp_path),
             "simplify_status": "candidates",
             "simplify_report": {"status": "candidates",
                                 "report": {"candidates": [_candidate().to_dict(),
                                                           {**_candidate("slot1").to_dict(),
                                                            "kind": "slot_end", "removable": False}]}}}
    state.update(over)
    return state


def _run(state) -> dict:
    return asyncio.run(node_simplify_attempt(state))


def _report() -> SimplifyReport:
    return SimplifyReport(
        candidates=(_candidate(),), summary="Removed 1 features.",
        operations=({"name": "remove_feature", "feature": "hole0", "status": "kept", "mutated": True,
                     "reason": "accepted", "estimate": -1e-5},),
        diagnostics={"verification": {"accepted": ["hole0"], "estimated_change": -1e-5, "spent": 1e-5},
                     "t0": {"valid": True, "volume_change": 1e-3}})


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_ENABLED", True)


@pytest.fixture
def logged(monkeypatch):
    rows: list = []

    class _Logger:
        def __init__(self, job_id):
            self.job_id = job_id

        def log(self, event_type, payload, **kw):
            rows.append((event_type, kw.get("op_id"), payload))

    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _Logger)
    return rows


@pytest.fixture
def simplifies(monkeypatch):
    calls = []

    def _simplify(source, destination, **kw):
        calls.append((str(source), str(destination), kw))
        destination.write_bytes(source.read_bytes() + b"\n/* simplified */\n")
        return _report()

    monkeypatch.setattr("meshpipeline.cad.simplify.simplify.simplify_step_file", _simplify)
    monkeypatch.setattr("meshpipeline.pipeline.simplify_promote.stages_for",
                        lambda geometry, engine="", budget_s=None: (True, ""))
    return calls


# IT SIMPLIFIES WHEN THE CHECK ACCEPTS


def test_an_accepted_simplification_is_what_gets_meshed(tmp_path, simplifies, logged):
    state = _state(tmp_path)
    before = state["geometry"]["ref"]["sha256"]

    out = _run(state)

    assert out["simplify_attempt"]["attempted"] is True
    assert out["simplify_attempt"]["status"] == "simplified"
    assert out["simplify_attempt"]["features_kept"] == ["hole0"]
    assert out["simplify_attempt"]["verification"]["estimated_change"] == -1e-5
    assert out["geometry"]["ref"]["sha256"] != before
    assert out["simplify_lineage"]["original"]["sha256"] == before
    assert out["simplify_lineage"]["features"] == ["hole0"]
    # inspection's verdict on the geometry as it arrived is not rewritten
    assert "simplify_status" not in out and "simplify_report" not in out
    assert [r[:2] for r in logged] == [("simplify_attempt", "simplify:attempt")]
    assert logged[0][2]["outcome"] == "simplified"


def test_only_inspections_removable_candidates_are_offered_and_written_beside_the_geometry(tmp_path, simplifies):
    _run(_state(tmp_path))
    source, destination, kw = simplifies[0]
    assert kw == {"feature_ids": ["hole0"]}
    assert destination.endswith("-simplified.step")
    assert destination.rsplit("/", 1)[0] == source.rsplit("/", 1)[0]


def test_the_derived_identity_names_the_simplification(tmp_path, simplifies):
    state = _state(tmp_path)
    out = _run(state)
    sid = out["geometry"]["ref"]["source_id"]
    assert sid.startswith(state["geometry"]["ref"]["source_id"] + ":simplified:")
    assert len(sid.rsplit(":", 1)[1]) == 16
    assert out["geometry"]["interpretation"]["unit"] == state["geometry"]["interpretation"]["unit"]


def test_a_repaired_part_that_is_simplified_carries_both_lineages(tmp_path, simplifies):
    repair_lineage = {"original": {"source_id": "upload-1", "sha256": "a" * 64}, "repaired": {}}
    state = _state(tmp_path, repair_lineage=repair_lineage)
    out = _run(state)
    assert "repair_lineage" not in out                      # untouched, so the state keeps it
    assert out["simplify_lineage"]["original"]["sha256"] == state["geometry"]["ref"]["sha256"]


# EVERY OTHER ENDING LEAVES THE GEOMETRY WHERE IT WAS


def test_switched_off_it_touches_nothing(tmp_path, monkeypatch, simplifies):
    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_ENABLED", False)
    out = _run(_state(tmp_path))
    assert out == {"simplify_attempt": {"attempted": False, "outcome": "off",
                                        "reason": "CAD simplification is switched off in this deployment"}}
    assert simplifies == []


@pytest.mark.parametrize("over, outcome", [
    ({"simplify_report": {}}, "no_report"),
    ({"simplify_status": "none"}, "no_candidates"),
    ({"simplify_status": "inconclusive"}, "no_candidates"),
])
def test_without_removable_candidates_nothing_is_attempted(tmp_path, simplifies, over, outcome):
    out = _run(_state(tmp_path, **over))
    assert out["simplify_attempt"]["outcome"] == outcome and "geometry" not in out
    assert simplifies == []


def test_a_triangle_file_is_not_rewritten(tmp_path, simplifies):
    out = _run(_state(tmp_path, geometry=geometry_state(tmp_path, filename="part.stl")))
    assert out["simplify_attempt"]["outcome"] == "format" and simplifies == []


def test_a_slow_simplification_is_inconclusive_on_its_clock(tmp_path, monkeypatch):
    import time

    from meshpipeline.engines import determinism as det

    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_TIMEOUT_S", 0.05)
    monkeypatch.setattr("meshpipeline.cad.simplify.simplify.simplify_step_file",
                        lambda *a, **k: time.sleep(0.5))
    with det.recording() as rec:
        out = _run(_state(tmp_path))
    assert out["simplify_attempt"]["outcome"] == "timeout"
    assert out["simplify_attempt"]["status"] == "inconclusive"
    assert "geometry" not in out
    assert rec.time_limited and rec.time_limited[0]["where"] == "cad_simplify"
    assert rec.time_limited[0]["rule"] == "TIME-6"


def test_a_refusal_is_recorded_with_its_verdicts_and_logged(tmp_path, monkeypatch, logged):
    def _refuse(*a, **k):
        raise SimplifyRefused("the simulation check accepted no removed feature within the budget",
                              measurements={"operations": [{"feature": "hole0", "status": "rejected"}]})

    monkeypatch.setattr("meshpipeline.cad.simplify.simplify.simplify_step_file", _refuse)
    out = _run(_state(tmp_path))
    assert out["simplify_attempt"]["outcome"] == "refused" and "geometry" not in out
    assert out["simplify_attempt"]["status"] == "refused"
    assert out["simplify_attempt"]["detail"]["measurements"]["operations"][0]["status"] == "rejected"
    assert logged[0][0] == "simplify_attempt" and logged[0][2]["outcome"] == "refused"


@pytest.mark.parametrize("exc, outcome", [
    (SimplifyUnavailable("CalculiX (ccx) is not installed"), "unavailable"),
    (RuntimeError("kernel exploded"), "failed"),
])
def test_our_own_failure_is_inconclusive_never_a_verdict(tmp_path, monkeypatch, exc, outcome):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr("meshpipeline.cad.simplify.simplify.simplify_step_file", _raise)
    out = _run(_state(tmp_path))
    assert out["simplify_attempt"]["outcome"] == outcome
    assert out["simplify_attempt"]["status"] == "inconclusive"
    assert "geometry" not in out


def test_geometry_that_does_not_stage_is_not_promoted(tmp_path, monkeypatch, simplifies):
    monkeypatch.setattr("meshpipeline.pipeline.simplify_promote.stages_for",
                        lambda geometry, engine="", budget_s=None: (False, "RuntimeError: empty"))
    out = _run(_state(tmp_path))
    assert out["simplify_attempt"]["outcome"] == "not_promoted"
    assert "geometry" not in out and "simplify_lineage" not in out
