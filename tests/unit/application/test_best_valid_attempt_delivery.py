# Responsibility: Verify a finished run delivers its best VALID attempt by the fixed measured ranking, under the engine that built it, and that a review never moves a run to another engine.
# Boundaries: application/review_delivery (candidates, ranking, the swap), the uploader's engine check, terminal status, the ladder's closing record and routing - replayed on job 0d0fc370's recorded facts.
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.application import final_result as fr
from meshpipeline.application.artifact_uploader import BundleContractError, resolve_engine
from meshpipeline.application.review_delivery import candidates, rank_key, select
from meshpipeline.pipeline import engine_fallback as lad

jlog = logging.getLogger(__name__)


# #
# job 0d0fc370 (shared dev, 2026-10-19), from the worker's own log
# #
#
#   03:35:28  snappy planner: balanced quality, 5 layers, max 7M cells
#   03:35:33  wake refined 6 body lengths downstream in 3 graded boxes (6.02 / 12 / 24.1 mm cells)
#   03:47:46  snappy attempt 1: cells=2345056 wall_faces=139861 skew_faces=0 -> PRODUCTION-GRADE
#   03:48:19  solvability PASS (2345056 cells, 23 iterations, residual 7.97e-07)
#   03:49:13  review: verdict=failed blocking=[] improve=[prism_layer_coverage]
#   03:49:14  engine_fallback: "snappy could not mesh this shape (fixable: review)" -> cfmesh
#   03:50:37  cfmesh attempt 2: 34470 cells, solvability PASS
#   03:51:19  review: blocking=[wake_resolution:requirement_absent], concern near_wall_layers
#   03:53:58  cfmesh attempt 3: 34382 cells, solvability PASS; review as attempt 2
#   03:54:29  delivered attempt 1's mesh with engine still 'cfmesh' -> upload refused 3 times:
#             "manifest engine 'snappy' disagrees with the execution's engine 'cfmesh'" -> FAILED
#
# Not in the log, so swept rather than assumed: attempt 1's worst non-orthogonality (snappy's own
# meshQualityControls hold it at the plan's maxNonOrtho 65) and every attempt's layer coverage
# (the review called attempt 1's partial, attempt 2/3's absent or partial).

JOB = "0d0fc370-5fbb-4da5-bae4-b16d613773fe"
PATCHES = [{"name": "car", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
WAKE = {"enabled": True, "reach_wake_lengths": 6.0,
        "tiers": [{"cell_m": 0.00602}, {"cell_m": 0.012}, {"cell_m": 0.0241}]}

#: attempt 1's review, as the reviewer recorded it: one concern, nothing that makes the mesh the
#: wrong problem, an improvement asked for
REVIEW_1 = [{"axis_key": "prism_layer_coverage", "passed": False, "attempt": 1, "blocking": "",
             "improve": True, "severity": "minor",
             "finding": "Prism layers cover only part of the car's wall."}]
#: attempts 2 and 3: the wake the brief needs is absent (a wrong-problem finding), layers a concern
REVIEW_CF = [{"axis_key": "wake_resolution", "passed": False, "blocking": "requirement_absent",
              "improve": True, "severity": "material",
              "finding": "No refinement downstream of the body."},
             {"axis_key": "near_wall_layers_generated", "passed": False, "blocking": "",
              "improve": True, "severity": "minor", "finding": "Layers did not grow on the wall."}]


def _workspace(root: Path, n: int, *, engine: str, cells: int, quality: dict) -> str:
    ws = root / f"attempt_{n}"
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    (ws / "constant" / "polyMesh" / "owner").write_text("owner")
    (ws / "mesh_manifest.json").write_text(json.dumps({
        "schema_version": "2.1", "mesh_mode": engine, "mesh_written": True,
        "engine_params": {}, "flow_topology": "external", "cell_count": cells,
        "mesh_units": "m", "patch_types": {"car": "wall", "farfield": "patch"},
        "patch_face_counts": {"car": 139861 if engine == "snappy" else 9000, "farfield": 600},
        "quality": {"cells": cells, "fatal": [], **quality}}))
    return str(ws)


def _entry(n: int, ws: str, engine: str, findings: list) -> dict:
    return {"attempt": n, "workspace": ws, "evidence_key": f"k{n}", "verdict": "FAIL",
            "findings": [dict(f, attempt=n) for f in findings], "feedback": "",
            "requirement_caveats": [], "engine": engine, "engine_params": {}}


def _replay_end(root: Path, *, snappy_non_ortho: float, snappy_cover: float,
                cf_non_ortho: float, cf_cover: float | None, recorded_engine: bool = True) -> dict:
    """The graph's final state exactly as job 0d0fc370 ended: on cfMesh's attempt 3, with every
    attempt's mesh on disk and its review in the history."""
    cf_q = {"max_non_ortho": cf_non_ortho,
            **({"layer_coverage_pct": cf_cover} if cf_cover is not None else {})}
    ws1 = _workspace(root, 1, engine="snappy", cells=2_345_056,
                     quality={"skew_fraction": 0.0, "max_non_ortho": snappy_non_ortho,
                              "layer_coverage_pct": snappy_cover, "wake_refinement": WAKE})
    ws2 = _workspace(root, 2, engine="cfmesh", cells=34_470, quality=cf_q)
    ws3 = _workspace(root, 3, engine="cfmesh", cells=34_382, quality=cf_q)
    history = [_entry(1, ws1, "snappy", REVIEW_1), _entry(2, ws2, "cfmesh", REVIEW_CF),
               _entry(3, ws3, "cfmesh", REVIEW_CF)]
    if not recorded_engine:
        # reviews recorded before the engine was kept on them: the manifest still names it
        for h in history:
            h.pop("engine"), h.pop("engine_params")
    return {
        "job_id": JOB, "engine": "cfmesh", "engine_source": lad.SOURCE_SUGGESTED,
        "engine_params": {}, "purpose": "external_cfd", "input_kind": "body-surface",
        "dimensionality": "3D", "intake_patches": [dict(p) for p in PATCHES],
        "request_txt": "External aerodynamics around the body, production-grade mesh.",
        "review_brief_txt": "", "retry_count": 3, "api_failure": "",
        "executor_success": True, "executor_failed_gate": "", "reviewer_verdict": "FAIL",
        "reviewer_axis_findings": [dict(f, attempt=3) for f in REVIEW_CF],
        "requirement_caveats": [], "user_dispute": {}, "openfoam_workspace": ws3,
        "mesh_manifest": json.loads((Path(ws3) / "mesh_manifest.json").read_text()),
        "review_history": history,
        "engine_ladder": {"approved": "snappy", "source": lad.SOURCE_SUGGESTED,
                          "attempts": [{"attempt": 1, "engine": "snappy", "kind": lad.FIXABLE,
                                        "cause": "review"},
                                       {"attempt": 2, "engine": "cfmesh", "kind": lad.FIXABLE,
                                        "cause": "review"}],
                          "switches": [{"attempt": 2, "from": "snappy", "to": "cfmesh",
                                        "because": "review", "kind": lad.FIXABLE}]},
    }


@pytest.mark.parametrize("snappy_non_ortho", [38.0, 64.9])
@pytest.mark.parametrize("snappy_cover,cf_cover", [(5.0, None), (62.0, 0.0), (90.0, 95.0)])
@pytest.mark.parametrize("cf_non_ortho", [30.0, 64.0])
def test_replay_0d0fc370_delivers_attempt_1_under_snappy(tmp_path, snappy_non_ortho,
                                                        snappy_cover, cf_cover, cf_non_ortho):
    end = _replay_end(tmp_path, snappy_non_ortho=snappy_non_ortho, snappy_cover=snappy_cover,
                      cf_non_ortho=cf_non_ortho, cf_cover=cf_cover)
    # the failure as it happened: the run's last engine against attempt 1's manifest
    with pytest.raises(BundleContractError, match="disagrees with the execution's engine"):
        resolve_engine(Path(end["review_history"][0]["workspace"]), end["engine"])

    picked = select(end)
    assert picked["openfoam_workspace"] == end["review_history"][0]["workspace"]
    assert picked["engine"] == "snappy", "the delivered attempt is not named by its own engine"
    assert picked["mesh_manifest"]["cell_count"] == 2_345_056
    assert picked["mesh_manifest"]["mesh_mode"] == "snappy"
    # THE UPLOAD THAT REFUSED THREE TIMES now passes its engine check
    assert resolve_engine(Path(picked["openfoam_workspace"]), picked["engine"]).name == "snappy"

    # delivered, with the review's concern listed - never failed, never called a pass
    outcome = fr.RunOutcome.from_graph_state(picked)
    assert fr.derive_terminal_status(outcome, job_id=JOB, jlog=jlog).succeeded
    concerns = fr.review_concerns_caveat(outcome)
    assert concerns and [i["axis_key"] for i in concerns["items"]] == ["prism_layer_coverage"]
    assert not fr.review_blocking(outcome)

    # the closing record: attempt 3 stays filed under cfMesh, the delivery under snappyHexMesh
    rec = lad.final_record(picked, succeeded=True, system_failure=False)
    assert rec["delivered_by"] == "snappy"
    assert {a["attempt"]: a["engine"] for a in rec["attempts"]} == {1: "snappy", 2: "cfmesh",
                                                                    3: "cfmesh"}
    assert rec["delivered_from"]["attempt"] == 1
    note = lad.delivered_note(rec)
    assert "attempt 1" in note and "snappyHexMesh" in note
    # and the MEASURED facts alone pick it: with every review finding taken away, attempts 2 and
    # 3 still miss the wake refinement the approved plan builds (and, with no layers, a limit)
    stripped = {**end, "reviewer_axis_findings": [],
                "review_history": [{**h, "findings": []} for h in end["review_history"]]}
    again = select(stripped)
    assert again["engine"] == "snappy"
    assert again["openfoam_workspace"] == picked["openfoam_workspace"]
    why = again["engine_ladder"]["delivered_from"]["why"]
    assert "review" not in why and ("plan" in why or "quality" in why), why


def test_replay_0d0fc370_reviews_recorded_without_their_engine_still_deliver_snappy(tmp_path):
    end = _replay_end(tmp_path, snappy_non_ortho=50.0, snappy_cover=62.0, cf_non_ortho=40.0,
                      cf_cover=None, recorded_engine=False)
    picked = select(end)
    assert picked["engine"] == "snappy"
    assert resolve_engine(Path(picked["openfoam_workspace"]), picked["engine"]).name == "snappy"


@pytest.fixture
def quiet(monkeypatch):
    import meshpipeline.pipeline.engine_select as es

    async def _pub(job_id, text, op_id):
        return None

    monkeypatch.setattr(es, "_publish", _pub)
    monkeypatch.setattr(es, "_log_selection", lambda *a, **k: None)
    monkeypatch.setattr(polcfg, "ENGINE_FALLBACK_ENABLED", True)
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 1)


def test_replay_0d0fc370_attempt_1_stays_on_snappy_and_its_concern_buys_one_retry(quiet,
                                                                                  tmp_path):
    from meshpipeline.pipeline.graph import route_after_reviewer
    ws1 = _workspace(tmp_path, 1, engine="snappy", cells=2_345_056,
                     quality={"skew_fraction": 0.0, "max_non_ortho": 50.0,
                              "layer_coverage_pct": 62.0, "wake_refinement": WAKE})
    after_1 = {"job_id": JOB, "engine": "snappy", "engine_source": lad.SOURCE_SUGGESTED,
               "engine_params": {}, "purpose": "external_cfd", "input_kind": "body-surface",
               "dimensionality": "3D", "intake_patches": [dict(p) for p in PATCHES],
               "request_txt": "External aerodynamics around the body, production-grade mesh.",
               "review_brief_txt": "", "retry_count": 1, "api_failure": "",
               "executor_success": True, "executor_failed_gate": "",
               "reviewer_verdict": "FAIL", "reviewer_axis_findings": [dict(f) for f in REVIEW_1],
               "requirement_caveats": [], "openfoam_workspace": ws1,
               "review_history": [_entry(1, ws1, "snappy", REVIEW_1)],
               "builder_deadline_epoch": 0.0, "pipeline_deadline_epoch": 0.0,
               "engine_ladder": {}}
    # the review asked for an improvement: one same-engine retry...
    assert route_after_reviewer(after_1) == "node_classifier"
    out = asyncio.run(lad.node_engine_fallback(after_1))
    assert "engine" not in out, "the review moved the run off snappyHexMesh again"
    assert out["engine_ladder"]["attempts"][-1]["kind"] == lad.REVIEW
    assert not out["engine_ladder"]["switches"]
    # ...and when attempt 2's review again leaves only concerns, the run ends and delivers the
    # better of the two (application/review_delivery)
    after_2 = {**after_1, "retry_count": 2, "engine_ladder": out["engine_ladder"],
               "reviewer_axis_findings": [dict(f, attempt=2) for f in REVIEW_1],
               "review_history": [*after_1["review_history"],
                                  _entry(2, str(tmp_path / "attempt_2"), "snappy", REVIEW_1)]}
    assert route_after_reviewer(after_2) == "__end__"


# #
# the fixed ranking, one rule at a time
# #

def _cand_state(root: Path, metas: list[dict], **over) -> dict:
    history = []
    for n, meta in enumerate(metas, start=1):
        engine = meta.pop("engine", "snappy")
        findings = meta.pop("findings", [])
        ws = _workspace(root, n, engine=engine, cells=1000 * n,
                        quality={"skew_fraction": 0.0, "wake_refinement": WAKE, **meta})
        history.append(_entry(n, ws, engine, findings))
    last = history[-1]
    s = {"job_id": "j", "engine": "snappy", "engine_source": lad.SOURCE_SUGGESTED,
         "engine_params": {}, "purpose": "external_cfd", "intake_patches": PATCHES,
         "request_txt": "External aero.", "review_brief_txt": "", "api_failure": "",
         "retry_count": len(metas), "executor_success": True, "executor_failed_gate": "",
         "reviewer_verdict": "FAIL", "reviewer_axis_findings": last["findings"],
         "requirement_caveats": [], "user_dispute": {}, "openfoam_workspace": last["workspace"],
         "review_history": history, "engine_ladder": {"approved": "snappy"}}
    s.update(over)
    return s


def _delivered(state: dict) -> int:
    picked = select(state)
    return next(h["attempt"] for h in state["review_history"]
                if h["workspace"] == picked["openfoam_workspace"])


OK = {"max_non_ortho": 50.0, "max_skewness": 2.0, "layer_coverage_pct": 80.0}


def test_rule_1_passing_every_limit_beats_a_better_shaped_mesh_that_misses_one(tmp_path):
    st = _cand_state(tmp_path, [dict(OK, max_non_ortho=70.0, max_skewness=0.5),
                                dict(OK, max_non_ortho=60.0)])
    assert _delivered(st) == 2


def test_rule_2_fewer_failed_checks_first(tmp_path):
    two = dict(OK, max_non_ortho=70.0, skew_fraction=0.01)
    one = dict(OK, max_non_ortho=80.0)
    assert _delivered(_cand_state(tmp_path, [two, one])) == 2


def test_rule_3_the_plan_requirements_met_come_before_cell_shape(tmp_path):
    no_wake = dict(OK, max_non_ortho=20.0, wake_refinement={"enabled": False, "tiers": []})
    assert _delivered(_cand_state(tmp_path, [no_wake, dict(OK)])) == 2
    bare = dict(OK, max_non_ortho=20.0, layer_coverage_pct=0.0)
    assert _delivered(_cand_state(tmp_path / "b", [bare, dict(OK)])) == 2


def test_rule_4_worst_non_orthogonality_then_skewness(tmp_path):
    assert _delivered(_cand_state(tmp_path, [dict(OK, max_non_ortho=55.0), dict(OK)])) == 2
    assert _delivered(_cand_state(tmp_path / "s", [dict(OK, max_skewness=3.0), dict(OK)])) == 2


def test_rule_5_layer_coverage(tmp_path):
    assert _delivered(_cand_state(tmp_path, [dict(OK, layer_coverage_pct=60.0), dict(OK)])) == 2


def test_rule_6_the_earlier_attempt_wins_a_tie(tmp_path):
    assert _delivered(_cand_state(tmp_path, [dict(OK), dict(OK)])) == 1


def test_the_reviews_concerns_are_listed_never_ranked(tmp_path):
    many = [{"axis_key": k, "passed": False, "blocking": "", "improve": True}
            for k in ("prism_layer_coverage", "wake_resolution", "surface_capture")]
    st = _cand_state(tmp_path, [dict(OK, findings=many), dict(OK, max_non_ortho=55.0)])
    picked = select(st)
    assert _delivered(st) == 1, "the mesh with fewer AI concerns won over the better-measured one"
    concerns = fr.review_concerns_caveat(fr.RunOutcome.from_graph_state(picked))
    assert concerns and len(concerns["items"]) == 3          # every concern is still said


def test_a_broken_or_failed_attempt_is_never_a_candidate(tmp_path):
    st = _cand_state(tmp_path, [dict(OK, fatal=["negative-volume cells"]), dict(OK)])
    assert [c.attempt for c in candidates(st)] == [2]
    # the run's own last attempt that failed a gate is no candidate either
    failed = {**st, "executor_success": False, "executor_failed_gate": "quality_floor",
              "openfoam_workspace": str(tmp_path / "attempt_3"),
              "review_history": st["review_history"][:1]}
    assert candidates(failed) == []
    assert select(failed) == failed


def test_rank_key_is_measured_facts_and_order_only(tmp_path):
    st = _cand_state(tmp_path, [dict(OK)])
    (c,) = candidates(st)
    assert rank_key(c) == (False, False, 0, 0, 50.0, 2.0, -80.0, 1)


def test_no_valid_mesh_fails_with_each_attempts_reason_and_the_users_options():
    st = {"job_id": "j", "engine": "snappy", "engine_source": lad.SOURCE_SUGGESTED,
          "engine_params": {}, "purpose": "external_cfd", "input_kind": "body-surface",
          "dimensionality": "3D", "intake_patches": PATCHES, "request_txt": "External aero.",
          "review_brief_txt": "", "retry_count": 2, "api_failure": "",
          "executor_success": False, "executor_failed_gate": "quality_floor",
          "executor_failure_cause": "mesh_quality", "reviewer_verdict": "",
          "review_history": [], "requirement_caveats": [], "user_dispute": {},
          "engine_ladder": {"approved": "snappy", "source": lad.SOURCE_SUGGESTED,
                            "attempts": [{"attempt": 1, "engine": "snappy", "kind": lad.FIXABLE,
                                          "cause": "mesh_quality",
                                          "reason": "its cells came out too badly shaped to "
                                                    "use"}],
                            "switches": []}}
    assert select(st) == st
    outcome = fr.RunOutcome.from_graph_state(st)
    decision = fr.derive_terminal_status(outcome, job_id="j", jlog=jlog)
    assert not decision.succeeded
    rec = lad.final_record(st, succeeded=False, system_failure=False)
    result = fr.with_engine_ladder(fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="", failed_gate="quality_floor",
        api_failure="", attempts=2, attempts_max=3, required_ready=False, delivered_types=[],
        optional_warnings=[], requirement_caveats=[], review_blocking=[]), rec)
    msg = fr.render_message(result)
    assert "What each attempt did:" in msg
    assert msg.count("Attempt 1 (snappyHexMesh)") == 1 and "Attempt 2 (snappyHexMesh)" in msg
    assert rec["offer"] and rec["offer"]["text"] in msg            # the user's way on
