# Responsibility: Verify an exported label comes from the declared section, excluding conflicted and foreign records.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.capture_authority import (
    CAPTURE_OWNER,
    FOREIGN_OWNER,
    seed_conflict,
    seed_events,
)
from tests.product_modes import set_modes

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.application.maintenance.export import _export  # noqa: E402
from meshpipeline.pipeline.enums import FailureSection  # noqa: E402

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


JOB = "j1"
SAMPLE = "sample_test"


def _ts(o: float = 0.0) -> str:
    return (datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=o)).isoformat()


def _classifier_ev(section: str, ts: float, *, summary: str = "bad cell quality",
                   gate: str = "manifest_valid", axes: list | None = None,
                   mode: str = "retry", attempt: int = 1) -> dict:
    return {"event_type": "classifier_run", "job_id": JOB, "timestamp": _ts(ts),
            "attempt": attempt, "payload": {
                "section": section, "summary": summary, "error_source": "executor_fail",
                "failed_gate": gate, "failed_axes": axes or [],
                "builder_mode": mode, "deterministic": True}}


def _events(classifier_evs: list[dict], *, verdict: str = "FAIL",
            exec_success: bool = False) -> list[dict]:
    return [
        {"event_type": "intake_complete", "job_id": JOB, "timestamp": _ts(0), "attempt": 0,
         "payload": {"intake_turns": [], "intake_system_snapshot": "s",
                     "intake_llm_metadata": [], "request_txt": "mesh it",
                     "review_brief_txt": "check", "max_turns_reached": False}},
        {"event_type": "engine_select_run", "job_id": JOB, "timestamp": _ts(0.5),
         "payload": {"chosen": "cfmesh", "source": "user"}},
        {"event_type": "builder_attempt", "job_id": JOB, "timestamp": _ts(1), "attempt": 1,
         "payload": {"builder_message_histories": [], "builder_tool_call_histories": [],
                     "builder_system_message_snapshots": "s", "builder_full_responses": "d",
                     "builder_call_metadata": [], "builder_pruning_events": [],
                     "builder_tools_definition": []}},
        {"event_type": "executor_run", "job_id": JOB, "timestamp": _ts(2), "attempt": 1,
         "payload": {"success": exec_success, "executor_stdouts": "", "executor_stderrs": "",
                     "attempt_log_snapshots": "", "mesh_manifest": {}}},
        *classifier_evs,
        {"event_type": "reviewer_run", "job_id": JOB, "timestamp": _ts(8), "attempt": 1,
         "payload": {"verdict": verdict, "tool_calls": 1, "reviewer_tool_call_histories": [],
                     "reviewer_reasoning_chains": [], "reviewer_full_responses": [],
                     "reviewer_system_snapshots": "s", "reviewer_input_texts": "i",
                     "reviewer_review_dirs": "/tmp/r"}},
        {"event_type": "final_result_built", "job_id": JOB, "timestamp": _ts(9), "attempt": 0,
         "payload": {"final_result": {"schema_version": 1, "status": "succeeded"}, "terminal_message": "Mesh generation completed successfully.", "geometry_source": _GEOMETRY_SOURCE,
                     "agent_model_configs": {}}},
    ]


def _run_export(capture_authority, tmp_path, monkeypatch, events: list[dict]) -> Path:
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path / "jobs", raising=False)
    set_modes(monkeypatch, collection=True)
    seed_events(capture_authority, CAPTURE_OWNER, JOB, events)
    _export(JOB, {"job_id": JOB}, owner_id=CAPTURE_OWNER)
    return tmp_path / JOB


def _labels(capture_authority, tmp_path, monkeypatch, events) -> dict:
    sample_dir = _run_export(capture_authority, tmp_path, monkeypatch, events)
    return [json.loads(x)["payload"]
            for x in (sample_dir / "events.jsonl").read_text().splitlines()
            if json.loads(x)["name"] == "job_summary"][-1]


def _episode(sample_dir: Path, pattern: str) -> dict:
    hits = sorted((sample_dir / "episodes").glob(pattern))
    assert hits, f"no episode matching {pattern!r}"
    return json.loads(hits[0].read_text())


def test_failure_reason_comes_from_the_declared_section(capture_authority, tmp_path, monkeypatch):
    lab = _labels(capture_authority, tmp_path, monkeypatch, _events([_classifier_ev(FailureSection.GEOMETRY, 3)]))
    assert lab["failure_reason"] == "geometry_error"


def test_failure_reason_uses_the_LAST_section_not_the_first(capture_authority, tmp_path, monkeypatch):
    lab = _labels(capture_authority, tmp_path, monkeypatch, _events([
        _classifier_ev(FailureSection.GEOMETRY, 3),
        _classifier_ev(FailureSection.LAYERS, 4, attempt=2),
    ]))
    assert lab["failure_reason"] == "layer_configuration"


def test_a_rebuild_is_labelled_wrong_topology(capture_authority, tmp_path, monkeypatch):
    lab = _labels(capture_authority, tmp_path, monkeypatch, _events([
        _classifier_ev(FailureSection.TOPOLOGY, 3, mode="rebuild"),
    ]))
    assert lab["failure_reason"] == "wrong_topology"


def test_sections_repaired_preserves_order_and_dedupes(capture_authority, tmp_path, monkeypatch):
    lab = _labels(capture_authority, tmp_path, monkeypatch, _events([
        _classifier_ev(FailureSection.MESH, 3),
        _classifier_ev(FailureSection.GEOMETRY, 4, attempt=2),
        _classifier_ev(FailureSection.MESH, 5, attempt=3),
    ]))
    assert lab["sections_repaired"] == ["MESH", "GEOMETRY"]


def test_a_successful_run_has_no_failure_reason(capture_authority, tmp_path, monkeypatch):
    lab = _labels(capture_authority, tmp_path, monkeypatch,
                  _events([], verdict="PASS", exec_success=True))
    assert lab["failure_reason"] == "none"
    assert lab["sections_repaired"] == []


def test_labels_are_not_silently_blank_when_the_classifier_ran(capture_authority, tmp_path, monkeypatch):
    lab = _labels(capture_authority, tmp_path, monkeypatch, _events([
        _classifier_ev(FailureSection.MESH, 3),
        _classifier_ev(FailureSection.GROUPS, 4, attempt=2),
    ]))
    assert lab["sections_repaired"], "a classified run must record which sections it repaired"
    assert lab["failure_reason"] == "patch_assignment"


def test_classifier_decision_is_a_deterministic_episode(capture_authority, tmp_path, monkeypatch):
    sample_dir = _run_export(capture_authority, tmp_path, monkeypatch, _events([
        _classifier_ev(FailureSection.MESH, 3, gate="sicn_floor",
                       summary="[QUALITY] min SICN 0.03 below the 0.1 floor"),
    ]))
    ep = _episode(sample_dir, "*classifier.attempt_1.json")
    assert ep["kind"] == "deterministic"          # no LLM turn, so no messages/tools
    assert "messages" not in ep
    d = ep["decision"]
    assert d["section"] == "MESH"
    assert d["failed_gate"] == "sicn_floor"
    assert d["builder_mode"] == "retry"
    assert d["error_source"] == "executor_fail"
    # the retry brief the builder actually received, verbatim
    assert "min SICN 0.03" in d["summary"]


def test_reviewer_rejection_records_the_failing_axes_on_the_classifier_episode(capture_authority, tmp_path, monkeypatch):
    sample_dir = _run_export(capture_authority, tmp_path, monkeypatch, _events([
        _classifier_ev(FailureSection.GROUPS, 3, gate="", axes=["farfield_clearance"],
                       summary="Far-field is 2 chords; the brief asked for 20."),
    ]))
    d = _episode(sample_dir, "*classifier.attempt_1.json")["decision"]
    assert d["failed_gate"] == ""
    assert d["failed_axes"] == ["farfield_clearance"]


def test_the_deliverable_and_two_layer_shape(capture_authority, tmp_path, monkeypatch):
    sample_dir = _run_export(capture_authority, tmp_path, monkeypatch, _events([], verdict="PASS", exec_success=True))
    assert (sample_dir / "episodes").is_dir()
    names = {p.name for p in sample_dir.iterdir()}
    for dead in ("sample.json", "sft", "geometry.step",
                 "labels.json", "record.json", "timeline.json", "attempts",
                 "reviews", "intake", "prompts", "mesh_bundle", "input.step"):
        assert dead not in names, f"retired-layout artifact {dead} still emitted"
    sample = [json.loads(x)["payload"]
              for x in (sample_dir / "events.jsonl").read_text().splitlines()
              if json.loads(x)["name"] == "job_summary"][-1]
    assert sample["schema_version"] == "4.0"
    assert sample["quality"] == "succeeded"


# labels under the durable authority

def test_an_identical_replay_does_not_produce_a_second_labelled_example(
        capture_authority, tmp_path, monkeypatch):
    events = _events([], verdict="PASS", exec_success=True)
    seed_events(capture_authority, CAPTURE_OWNER, JOB, events)
    seed_events(capture_authority, CAPTURE_OWNER, JOB, events)          # the takeover replays

    rows = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=JOB)
    assert len(rows) == len(events), "a replay produced a second training example"


def test_distinct_operations_with_identical_payloads_stay_distinct(
        capture_authority, tmp_path, monkeypatch):
    for op in ("builder:1", "builder:2"):
        capture_authority.record_operation(
            owner_id=CAPTURE_OWNER, job_id=JOB, execution_generation=1, op_key=op,
            name="builder_attempt", payload={"identical": True})
    assert len(capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=JOB)) == 2


def test_a_conflicted_operation_is_excluded_from_the_exported_labels(
        capture_authority, tmp_path, monkeypatch):
    events = _events([], verdict="PASS", exec_success=True)
    sample = _run_export(capture_authority, tmp_path, monkeypatch, events)
    before = len((sample / "events.jsonl").read_text().splitlines())

    seed_conflict(capture_authority, CAPTURE_OWNER, JOB, op_key="disputed",
                  name="builder_attempt")
    _export(JOB, {"job_id": JOB}, owner_id=CAPTURE_OWNER)

    after = (sample / "events.jsonl").read_text().splitlines()
    assert len(after) == before, "a disputed operation reached the exported labels"
    assert all("disputed" not in ln for ln in after)


def test_labels_do_not_mix_across_tenants(capture_authority, tmp_path, monkeypatch):
    seed_events(capture_authority, CAPTURE_OWNER, JOB,
                _events([], verdict="PASS", exec_success=True))
    seed_events(capture_authority, FOREIGN_OWNER, JOB,
                _events([], verdict="FAIL", exec_success=False))

    mine = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=JOB)
    theirs = capture_authority.trusted_operations(owner_id=FOREIGN_OWNER, job_id=JOB)

    # Identical CONTENT across tenants is fine and expected - digests would match. What must not
    # happen is one tenant's read returning the other's rows, so compare the verdicts each side
    # actually gets back.
    assert [r["payload"].get("verdict") for r in mine if "verdict" in r["payload"]] == ["PASS"]
    assert [r["payload"].get("verdict") for r in theirs if "verdict" in r["payload"]] == ["FAIL"]
    assert len(mine) == len(theirs) == len(_events([], verdict="PASS", exec_success=True))


def test_the_exported_projection_preserves_the_durable_order_and_semantics(
        capture_authority, tmp_path, monkeypatch):
    events = _events([], verdict="PASS", exec_success=True)
    sample = _run_export(capture_authority, tmp_path, monkeypatch, events)
    rendered = [json.loads(x) for x in (sample / "events.jsonl").read_text().splitlines()]

    assert [r["seq"] for r in rendered] == sorted(r["seq"] for r in rendered)
    assert all(r["record_type"] == "span_event" for r in rendered)
    # type/name/kind survive the round trip, which is what classification reads
    assert {r["name"] for r in rendered} >= {e["event_type"] for e in events}
