# Responsibility: Verify every exported field is reconstructed from durable events, falling back only where declared.
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.capture_authority import CAPTURE_OWNER, seed_events

import meshpipeline.settings.env as envcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.capture.events import TrainingEvent
from meshpipeline.capture.source import events_to_state, select_export_source

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


_TS = datetime(2024, 1, 1, tzinfo=UTC)


def _ev(event_type: str, payload: dict, attempt: int | None = None) -> TrainingEvent:
    return TrainingEvent(
        timestamp=_TS,
        job_id="job-test",
        event_type=event_type,
        attempt=attempt,
        payload=payload,
    )


def _engine_ev() -> TrainingEvent:
    return TrainingEvent(
        timestamp=_TS, job_id="job-test", event_type="engine_select_run",
        attempt=None, payload={"chosen": "cfmesh", "source": "selector"},
    )


def _intake_ev(**extra) -> TrainingEvent:
    base = {
        "intake_turns":             [{"role": "user", "content": "mesh it"}],
        "intake_system_snapshot":   "intake system",
        "intake_llm_metadata":      [{"finish_reason": "stop"}],
        "request_txt":              "make a mesh",
        "review_brief_txt":         "do it well",
        "domain":                   "external_aero",
        "max_turns_reached":        False,
    }
    base.update(extra)
    return _ev("intake_complete", base)


def _builder_ev(attempt: int = 1, **extra) -> TrainingEvent:
    base = {
        "builder_message_histories":        [{"role": "user", "content": "build"}],
        "builder_tool_call_histories":      [{"tool": "write_file"}],
        "builder_system_message_snapshots": "builder system",
        "builder_full_responses":           "mesh generated",
        "builder_tools_definition":         [{"name": "write_file"}],
    }
    base.update(extra)
    return _ev("builder_attempt", base, attempt=attempt)


def _executor_ev(attempt: int = 1, success: bool = True, **extra) -> TrainingEvent:
    base = {
        "executor_stdouts":       "OpenFOAM OK",
        "executor_stderrs":       "",
        "attempt_log_snapshots":  "attempt_log content",
        "mesh_manifest":          {"cells": 1000},
        "success":                success,
    }
    base.update(extra)
    return _ev("executor_run", base, attempt=attempt)


def _classifier_ev(attempt: int = 1, **extra) -> TrainingEvent:
    base = {
        "section":       "MESH",
        "summary":       "bad cell quality",
        "failed_gate":   "manifest_valid",
        "failed_axes":   [],
        "error_source":  "mesh_quality",
        "builder_mode":  "retry",
        "deterministic": True,
    }
    base.update(extra)
    return _ev("classifier_run", base, attempt=attempt)


def _reviewer_ev(verdict: str = "PASS", attempt: int = 1, **extra) -> TrainingEvent:
    base = {
        "reviewer_tool_call_histories": [{"tool": "read_file"}],
        "reviewer_reasoning_chains":    ["looks good"],
        "reviewer_full_responses":      [f"<<{verdict}>>"],
        "reviewer_system_snapshots":    "reviewer system",
        "reviewer_input_texts":         "review input",
        "reviewer_review_dirs":         "/tmp/review_1",
        "verdict":                      verdict,
        "tool_calls":                   1,
    }
    base.update(extra)
    return _ev("reviewer_run", base, attempt=attempt)


def _terminal_ev(**extra) -> TrainingEvent:
    base = {
        "final_result": {"schema_version": 1, "job_id": "j", "owner_id": "o",
                         "status": "succeeded", "outcome_code": "success"},
        "terminal_message":    "Mesh generation was successful.",
        "geometry_source":      _GEOMETRY_SOURCE,
        "agent_model_configs": {"builder": "model-x"},
        "api_failure":         False,
    }
    base.update(extra)
    return _ev("final_result_built", base)


def _successful_job_events() -> list[TrainingEvent]:
    return [
        _intake_ev(),
        _engine_ev(),
        _builder_ev(attempt=1),
        _executor_ev(attempt=1, success=True),
        _reviewer_ev(verdict="PASS", attempt=1),
        _terminal_ev(),
    ]


def _retry_job_events() -> list[TrainingEvent]:
    return [
        _intake_ev(),
        _engine_ev(),
        _builder_ev(attempt=1),
        _executor_ev(attempt=1, success=False),
        _classifier_ev(attempt=1),
        _builder_ev(attempt=2),
        _executor_ev(attempt=2, success=True),
        _reviewer_ev(verdict="PASS", attempt=2),
        _terminal_ev(),
    ]


_FALLBACK = {
    "openfoam_workspace": "/tmp/ws/attempt_1",
    "job_id":             "job-test",
    "user_id":            "user-42",
    "api_failure":        "",
    "session_id":         "sess-1",
    "executor_output":    "ok",
    "mesh_manifest":      {"cells": 999},
    "builder_noop_count": 0,
    "builder_mode":       "standard",
    "domain":             "external_aero",
    "geometry_source":     _GEOMETRY_SOURCE,
    "agent_model_configs": {"builder": "model-x"},
}



class TestEventsToStateIntake:

    def test_intake_max_turns_reached_true(self):
        evs = [_intake_ev(max_turns_reached=True)] + _successful_job_events()[1:]
        s = events_to_state(evs, _FALLBACK)
        assert s["intake_max_turns_reached"] is True

    def test_domain_from_intake_event(self):
        s = events_to_state(_successful_job_events(), _FALLBACK)
        assert s["domain"] == "external_aero"

    def test_domain_falls_back_to_state_when_no_intake(self):
        evs = [e for e in _successful_job_events() if e.event_type != "intake_complete"]
        fb = {**_FALLBACK, "domain": "internal_aero"}
        s = events_to_state(evs, fb)
        assert s["domain"] == "internal_aero"


class TestEventsToStateBuilder:

    def test_builder_llm_calls_is_absent_without_a_canonical_record(self):
        s = events_to_state(_successful_job_events(), _FALLBACK)
        assert s["builder_llm_calls"] == [None], "unknown, never a fabricated zero"

    def test_builder_tools_definition_taken_from_last_event(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert s["builder_tools_definition"] == [{"name": "write_file"}]

    def test_builder_message_histories_two_attempts(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert len(s["builder_message_histories"]) == 2

    def test_retry_count_equals_builder_event_count(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert s["retry_count"] == 2

class TestEventsToStateExecutor:

    def test_executor_success_scalar_from_last(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert s["executor_success"] is True

    def test_attempt_log_snapshots_one_for_two_attempts(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert len(s["attempt_log_snapshots"]) == 1
        assert s["attempt_log_snapshots"][0] == "attempt_log content"

    def test_executor_successes_retry_job(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert s["executor_successes"] == [False, True]


class TestEventsToStateClassifier:

    def test_a_retry_reconstructs_every_classifier_field(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        expected = {
            "classifier_sections":      ["MESH"],
            "classifier_summaries":     ["bad cell quality"],   # the verbatim builder brief
            "classifier_failed_gates":  ["manifest_valid"],
            "classifier_failed_axes":   [[]],
            "classifier_builder_modes": ["retry"],
            "classifier_error_sources": ["mesh_quality"],
            "classifier_result":        {"section": "MESH"},    # from the LAST event
        }
        for field, value in expected.items():
            assert s[field] == value, f"{field}: {s[field]!r} != {value!r}"

class TestEventsToStateReviewer:

    def test_reviewer_verdict_fail(self):
        evs = [
            _intake_ev(), _builder_ev(attempt=1),
            _executor_ev(attempt=1, success=False),
            _reviewer_ev(verdict="FAIL", attempt=1),
            _terminal_ev(),
        ]
        s = events_to_state(evs, _FALLBACK)
        assert s["reviewer_verdict"] == "FAIL"

    def test_reviewer_feedback_set_on_fail(self):
        evs = [
            _intake_ev(), _builder_ev(attempt=1),
            _executor_ev(attempt=1, success=False),
            _reviewer_ev(verdict="FAIL", attempt=1,
                         reviewer_reasoning_chains=["bad geometry"]),
            _terminal_ev(),
        ]
        s = events_to_state(evs, _FALLBACK)
        assert s["reviewer_feedback"] == "bad geometry"


class TestEventsToStateOperationalFallback:

    def test_operational_keys_pass_through_from_the_fallback_state(self):
        s = events_to_state(_successful_job_events(), _FALLBACK)
        for key in ("openfoam_workspace", "job_id", "user_id", "session_id",
                    "executor_output", "builder_noop_count", "builder_mode"):
            assert s[key] == _FALLBACK[key], f"{key} did not come from the fallback state"

    def test_empty_events_uses_all_fallback(self):
        s = events_to_state([], _FALLBACK)
        assert s["job_id"] == "job-test"
        assert s["intake_turns"] == []
        assert s["builder_message_histories"] == []
        assert s["executor_successes"] == []
        assert s["executor_success"] is False
        assert s["retry_count"] == 0



class TestSelectExportSource:

    def _write_events_jsonl(self, authority, job_id: str, events: list[dict]) -> None:
        seed_events(authority, CAPTURE_OWNER, job_id, events)

    def _full_event_dicts(self, events: list[TrainingEvent] | None = None) -> list[dict]:
        raw_events = events if events is not None else _retry_job_events()
        return [
            {
                "timestamp": _TS.isoformat(),
                "job_id":    e.job_id,
                "event_type": e.event_type,
                "attempt":   e.attempt,
                "payload":   e.payload,
            }
            for e in raw_events
        ]

    def test_uses_events_when_export_ready(self, capture_authority):
        self._write_events_jsonl(capture_authority, "job-ready", self._full_event_dicts())
        state, source = select_export_source("job-ready", _FALLBACK, owner_id=CAPTURE_OWNER)
        assert source == "events"
        assert state is not _FALLBACK

    def test_event_source_intake_turns_populated(self, capture_authority):
        self._write_events_jsonl(capture_authority, "job-ready2", self._full_event_dicts())
        state, source = select_export_source("job-ready2", _FALLBACK, owner_id=CAPTURE_OWNER)
        assert source == "events"
        assert state["intake_turns"] == [{"role": "user", "content": "mesh it"}]



class TestEventsPipelineStateParity:

    def _pipeline_state_successful(self) -> dict:
        return {
            **_FALLBACK,
            "intake_turns":             [{"role": "user", "content": "mesh it"}],
            "intake_system_snapshot":   "intake system",
            "intake_llm_metadata":      [{"finish_reason": "stop"}],
            "intake_max_turns_reached": False,
            "request_txt":              "make a mesh",
            "review_brief_txt":         "do it well",
            "builder_tools_definition": [{"name": "write_file"}],
            "builder_message_histories":        [[{"role": "user", "content": "build"}]],
            "builder_tool_call_histories":      [[{"tool": "write_file"}]],
            "builder_system_message_snapshots": ["builder system"],
            "builder_full_responses":           ["mesh generated"],
            "builder_input_messages":           [],
            "executor_successes":        [True],
            "executor_success":          True,
            "executor_stdouts":          ["OpenFOAM OK"],
            "executor_stderrs":          [""],
            "attempt_log_snapshots":     [],
            "classifier_sections":              [],
            "classifier_summaries":             [],
            "classifier_builder_modes":         [],
            "classifier_failed_gates":          [],
            "classifier_failed_axes":           [],
            "classifier_error_sources":         [],
            "classifier_result":                {},
            "reviewer_tool_call_histories":     [[{"tool": "read_file"}]],
            "reviewer_system_snapshots":        ["reviewer system"],
            "reviewer_review_dirs":             ["/tmp/review_1"],
            "reviewer_reasoning_chains":        [["looks good"]],
            "reviewer_full_responses":          [["<<PASS>>"]],
            "reviewer_input_texts":             ["review input"],
            "reviewer_verdict":                 "PASS",
            "reviewer_feedback":                "",
            "reviewer_result":                  "<<PASS>>",
            "reviewer_patch_checks":            {},
            "reviewer_axis_findings": [], # canonical list
            "reviewer_rebuild_required":        False,
            "reviewer_tool_calls":              [1],
            "final_result": {"schema_version": 1, "job_id": "j", "owner_id": "o",
                             "status": "succeeded", "outcome_code": "success"},
            "outcome_message":                  "Mesh generation was successful.",
            "geometry_source":                   _GEOMETRY_SOURCE,
            "agent_model_configs":              {"builder": "model-x"},
            "retry_count":                      1,
        }

    def test_successful_job_all_content_fields_match(self):
        ps = self._pipeline_state_successful()
        ev = events_to_state(_successful_job_events(), _FALLBACK)

        content_fields = [
            "intake_turns", "intake_system_snapshot", "intake_llm_metadata",
            "intake_max_turns_reached", "request_txt", "review_brief_txt",
            "builder_tools_definition",
            "builder_message_histories", "builder_tool_call_histories",
            "builder_system_message_snapshots", "builder_full_responses",
            "builder_input_messages",
            "executor_successes", "executor_success",
            "executor_stdouts", "executor_stderrs", "attempt_log_snapshots",
            "classifier_sections", "classifier_summaries",
            "classifier_builder_modes", "classifier_failed_gates",
            "classifier_failed_axes", "classifier_error_sources",
            "classifier_result",
            "reviewer_tool_call_histories", "reviewer_system_snapshots",
            "reviewer_review_dirs", "reviewer_reasoning_chains",
            "reviewer_full_responses", "reviewer_input_texts",
            "reviewer_verdict", "reviewer_feedback", "reviewer_result",
            "reviewer_patch_checks", "reviewer_axis_findings",
            "reviewer_rebuild_required", "reviewer_tool_calls",
            "final_result",
            "outcome_message",
            "geometry_source", "agent_model_configs",
            "retry_count",
        ]
        for field in content_fields:
            assert ev[field] == ps[field], (
                f"Mismatch for '{field}':\n"
                f"  events_to_state: {ev[field]!r}\n"
                f"  pipeline_state:  {ps[field]!r}"
            )

    def test_retry_job_attempt_log_snapshots_has_n_minus_one_entries(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert len(s["attempt_log_snapshots"]) == 1

    def test_retry_job_executor_successes_length_matches_executor_events(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert len(s["executor_successes"]) == 2

    def test_retry_job_classifier_arrays_length_matches_classifier_events(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert len(s["classifier_sections"]) == 1
        assert len(s["classifier_summaries"]) == 1
        assert len(s["classifier_failed_gates"]) == 1

    def test_retry_job_builder_arrays_length_matches_builder_events(self):
        s = events_to_state(_retry_job_events(), _FALLBACK)
        assert len(s["builder_message_histories"]) == 2
        assert len(s["builder_tool_call_histories"]) == 2



class TestExportConversationSampleWiring:

    def _make_full_state(self, tmp_path: Path) -> dict:
        return {
            **_FALLBACK,
            "openfoam_workspace": str(tmp_path / "attempt_1"),
            "intake_turns":             [{"role": "user", "content": "mesh it"}],
            "intake_system_snapshot":   "intake system",
            "intake_llm_metadata":      [],
            "intake_max_turns_reached": False,
            "request_txt":              "make a mesh",
            "review_brief_txt":         "do it well",
            "builder_tools_definition": [{"name": "write_file"}],
            "builder_message_histories":        [[{"role": "user", "content": "build"}]],
            "builder_tool_call_histories":      [[{"tool": "write_file"}]],
            "builder_system_message_snapshots": ["builder system"],
            "builder_full_responses":           ["mesh generated"],
            "builder_input_messages":           [],
            "executor_successes":        [True],
            "executor_success":          True,
            "executor_stdouts":          ["OpenFOAM OK"],
            "executor_stderrs":          [""],
            "attempt_log_snapshots":     [],
            "classifier_sections":              [],
            "classifier_summaries":             [],
            "classifier_builder_modes":         [],
            "classifier_failed_gates":          [],
            "classifier_failed_axes":           [],
            "classifier_error_sources":         [],
            "classifier_result":                {},
            "reviewer_tool_call_histories":     [[{"tool": "read_file"}]],
            "reviewer_system_snapshots":        ["reviewer system"],
            "reviewer_review_dirs":             [],
            "reviewer_reasoning_chains":        [["looks good"]],
            "reviewer_full_responses":          [["<<PASS>>"]],
            "reviewer_input_texts":             ["review input"],
            "reviewer_verdict":                 "PASS",
            "reviewer_feedback":                "",
            "reviewer_result":                  "<<PASS>>",
            "reviewer_patch_checks":            {},
            "reviewer_axis_findings": [], # canonical list
            "reviewer_rebuild_required":        False,
            "reviewer_tool_calls":              [1],
            "final_result": {"schema_version": 1, "job_id": "j", "owner_id": "o",
                             "status": "succeeded", "outcome_code": "success"},
            "outcome_message":                  "Mesh generation was successful.",
            "retry_count":                      1,
        }

    def _mock_redis_module(self, sample_num: int) -> MagicMock:
        mock_client = MagicMock()
        mock_client.incr.return_value = sample_num
        mock_module = MagicMock()
        mock_module.from_url.return_value = mock_client
        return mock_module

    def test_export_fails_when_no_events(self, tmp_path, monkeypatch):
        from meshpipeline.capture.source import ExportSourceError
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
        monkeypatch.setattr(envcfg, "PROMPTS_DIR", tmp_path)

        state = self._make_full_state(tmp_path)

        with patch.dict(sys.modules, {"redis": self._mock_redis_module(1)}):
            from meshpipeline.application.maintenance.export import _export
            with pytest.raises(ExportSourceError):
                _export("job-no-events", state)

    def test_export_uses_events_when_export_ready(self, capture_authority, tmp_path, monkeypatch):
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
        monkeypatch.setattr(envcfg, "PROMPTS_DIR", tmp_path)

        job_id = "job-events-ready"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, [
            {"event_type": e.event_type, "job_id": e.job_id, "timestamp": _TS.isoformat(),
             "attempt": e.attempt, "payload": e.payload}
            for e in _retry_job_events()
        ])

        state = self._make_full_state(tmp_path)

        with patch.dict(sys.modules, {"redis": self._mock_redis_module(2)}):
            from meshpipeline.application.maintenance.export import _export
            result = _export(job_id, state, owner_id=CAPTURE_OWNER)

        assert result["status"] == "ok"
        sample_dir = tmp_path / result["job_id"]
        # the intake requirements now live on the intake episode's output
        intake_eps = sorted((sample_dir / "episodes").glob("*_intake.json"))
        assert intake_eps, "no intake episode written"
        import json as _json
        ep = _json.loads(intake_eps[0].read_text())
        assert ep["output"]["request_txt"] == "make a mesh"
