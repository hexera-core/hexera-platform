# Responsibility: Verify each intake turn is captured in sequence with its usage, and the export handles a bad source.
from __future__ import annotations

import ast
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tests.product_modes import set_modes

import meshpipeline.settings.runtime as rtcfg

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

_mock_celery_mod = MagicMock()
sys.modules.setdefault("celery", _mock_celery_mod)

_mock_redis_mod = MagicMock()
_mock_redis_client = MagicMock()
_mock_redis_client.incr.return_value = 1
_mock_redis_mod.from_url.return_value = _mock_redis_client
sys.modules.setdefault("redis", _mock_redis_mod)



def _source(rel: str) -> str:
    return (APP_DIR / rel).read_text(encoding="utf-8")


def _make_training_event(event_type: str, payload: dict, ts: datetime | None = None):
    from meshpipeline.capture.events import TrainingEvent
    return TrainingEvent(
        timestamp=ts or datetime(2024, 6, 1, 12, 0, 0),
        job_id="test-job",
        event_type=event_type,
        attempt=None,
        payload=payload,
    )


def _minimal_events(extra_intake_turns: int = 0) -> list:
    events = []

    for t in range(extra_intake_turns):
        events.append(_make_training_event("intake_turn", {
            "turn":              t + 1,
            "finish_reason":     "stop",
            "usage":             {"prompt_tokens": 50 + t, "completion_tokens": 20, "total_tokens": 70 + t},
            "max_turns_reached": False,
        }))

    final_turn = extra_intake_turns + 1
    events.append(_make_training_event("intake_complete", {
        "intake_turns":           [{"role": "user", "content": "build a mesh"}],
        "intake_system_snapshot": "system prompt text",
        "intake_llm_metadata":    [{
            "turn": final_turn, "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
            "completed": True,
        }],
        "request_txt":       "please build a rocket mesh at Mach 2",
        "review_brief_txt":  "ensure domain is 10 body lengths",
        "domain":            "supersonic rocket external aerodynamics",
        "max_turns_reached": False,
    }))

    events.append(_make_training_event("builder_attempt", {
        "builder_message_histories":        [{"role": "user", "content": "build it"}],
        "builder_tool_call_histories":      [{"name": "run_python", "result": "ok"}],
        "builder_system_message_snapshots": "builder system prompt",
        "builder_full_responses":           "mesh generated",
        "builder_call_metadata":            [{"finish_reason": "stop"}],
        "builder_pruning_events":           [],
        "builder_tools_definition":         [{"type": "function"}],
    }))

    events.append(_make_training_event("executor_run", {
        "executor_stdouts":      "OpenFOAM ok",
        "executor_stderrs":      "",
        "attempt_log_snapshots": "attempt 1 log",
        "mesh_manifest":         {"cells": 100000},
        "success":               True,
    }))

    events.append(_make_training_event("reviewer_run", {
        "reviewer_tool_call_histories": [],
        "reviewer_reasoning_chains":    ["mesh looks good"],
        "reviewer_full_responses":      ["PASS - quality acceptable"],
        "reviewer_system_snapshots":    "reviewer prompt",
        "reviewer_input_texts":         "review context",
        "reviewer_review_dirs":         "/tmp/review_1",
        "verdict":                      "PASS",
    }))

    events.append(_make_training_event("final_result_built", {
        "final_result": {"schema_version": 1, "status": "succeeded"}, "terminal_message": "Mesh generation completed successfully.",
        "geometry_source":         _GEOMETRY_SOURCE,
        "agent_model_configs":    {"builder": {"model": "kimi-k2"}},
    }))

    return events



class TestIntakeLlmMetadataDeadCodeRemoved:

    def test_intake_llm_metadata_still_used_in_event_payload(self):
        src = _source("agents/intake/turn.py")
        assert '"intake_llm_metadata"' in src



class TestIntakeTurnEventUsageField:

    @staticmethod
    def _find_intake_turn_payload(tree) -> set:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            kv = {
                k.value: v
                for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant)
            }
            if kv.get("type") is None:
                continue
            type_node = kv["type"]
            if not (isinstance(type_node, ast.Constant) and type_node.value == "intake_turn"):
                continue
            payload_node = kv.get("payload")
            if payload_node is None or not isinstance(payload_node, ast.Dict):
                continue
            return {k.value for k in payload_node.keys if isinstance(k, ast.Constant)}
        return set()

    def test_intake_turn_payload_includes_usage_key(self):
        src = _source("agents/intake/turn.py")
        tree = ast.parse(src)
        keys = self._find_intake_turn_payload(tree)
        assert "usage" in keys, (
            "intake_turn payload must include a 'usage' key. "
            f"Found keys: {keys}"
        )

    def test_intake_turn_payload_has_required_keys(self):
        src = _source("agents/intake/turn.py")
        tree = ast.parse(src)
        keys = self._find_intake_turn_payload(tree)
        assert keys, "intake_turn payload dict not found in agents/intake/turn.py"
        assert "turn"            in keys
        assert "finish_reason"   in keys
        assert "max_turns_reached" in keys
        assert "usage"           in keys



class TestEventsToStateIntakeLlmMetadata:
    def test_single_turn_job_metadata(self):
        from meshpipeline.capture.source import events_to_state
        events = _minimal_events(extra_intake_turns=0)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert len(meta) == 1
        assert meta[0]["completed"] is True
        assert meta[0]["turn"] == 1

    def test_multi_turn_job_metadata_has_all_turns(self):
        from meshpipeline.capture.source import events_to_state
        events = _minimal_events(extra_intake_turns=2)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert len(meta) == 3

    def test_intermediate_turns_have_completed_false(self):
        from meshpipeline.capture.source import events_to_state
        events = _minimal_events(extra_intake_turns=2)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert meta[0]["completed"] is False
        assert meta[1]["completed"] is False

    def test_final_turn_has_completed_true(self):
        from meshpipeline.capture.source import events_to_state
        events = _minimal_events(extra_intake_turns=2)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert meta[-1]["completed"] is True

    def test_turn_numbers_are_sequential(self):
        from meshpipeline.capture.source import events_to_state
        events = _minimal_events(extra_intake_turns=2)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert [m["turn"] for m in meta] == [1, 2, 3]

    def test_usage_field_preserved_from_turn_events(self):
        from meshpipeline.capture.source import events_to_state
        events = _minimal_events(extra_intake_turns=1)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert meta[0]["usage"] == {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}

    def test_no_intake_events_gives_empty_list(self):
        from meshpipeline.capture.source import events_to_state
        events = [e for e in _minimal_events() if e.event_type != "intake_complete"]
        state = events_to_state(events, {"job_id": "test-job"})
        assert state["intake_llm_metadata"] == []

    def test_max_turns_reached_field_preserved(self):
        from meshpipeline.capture.source import events_to_state
        turn_event = _make_training_event("intake_turn", {
            "turn": 1, "finish_reason": "stop",
            "usage": None, "max_turns_reached": True,
        })
        events = _minimal_events(extra_intake_turns=0)
        events.insert(0, turn_event)
        state = events_to_state(events, {"job_id": "test-job"})
        meta = state["intake_llm_metadata"]
        assert meta[0]["max_turns_reached"] is True

class TestExportSourceErrorObservability:
    # The contract is what the task RETURNS, so it is driven through the task. The previous
    # version read the handler out of the source and asserted on its AST: a rename of the
    # exception, a moved return or a reworded status could not fail it, and a handler that
    # returned the right shape while never running would pass.

    def _run_export(self, tmp_path, monkeypatch, job_id: str):
        monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False)
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.maintenance.export import export_conversation_sample
        return export_conversation_sample(job_id, {"job_id": job_id})

    def test_an_unreadable_source_is_reported_as_export_source_error(self, tmp_path, monkeypatch):
        result = self._run_export(tmp_path, monkeypatch, "no-such-job")
        assert result["status"] == "export_source_error", result
        assert result["job_id"] == "no-such-job"
        assert result["reason"], "the failure carries no reason a reader could act on"

    def test_an_unexpected_failure_is_reported_separately_from_a_bad_source(
            self, tmp_path, monkeypatch):
        # The two handlers must stay distinguishable: one says the source could not be read,
        # the other that something else went wrong, and an operator triages on that difference.
        import meshpipeline.application.maintenance.export as export_mod

        monkeypatch.setattr(export_mod, "_export",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")))
        result = self._run_export(tmp_path, monkeypatch, "job-x")
        assert result["status"] == "error", result
        assert result["job_id"] == "job-x"
        assert "disk gone" not in str(result), "an internal failure message reached the caller"

    def test_export_source_error_propagates_from_export_impl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False)
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
        set_modes(monkeypatch, collection=True)

        from meshpipeline.application.maintenance.export import _export
        from meshpipeline.capture.source import ExportSourceError

        with pytest.raises(ExportSourceError):
            _export("no-such-job", {"job_id": "no-such-job"})
