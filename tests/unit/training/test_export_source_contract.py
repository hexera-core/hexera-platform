# Responsibility: Verify the export reads the durable authority, refuses an incomplete log, and never crosses tenants.
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import meshpipeline.settings.env as envcfg
import meshpipeline.settings.runtime as rtcfg

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from tests.capture_authority import (
    CAPTURE_OWNER,
    FOREIGN_OWNER,
    seed_conflict,
    seed_events,
)

from meshpipeline.capture import source as _source_mod
from meshpipeline.capture.source import ExportSourceError, select_export_source

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


# The name of the logger these tests capture, taken from the module itself so it can never drift
# from the real name again. It is `meshpipeline.capture.source` (logging.getLogger(__name__)), NOT
# the bare `capture.source` an earlier package layout used: caplog.at_level(logger=<name>) only
# lowers the threshold of the logger it NAMES, and the success line is INFO - below the root
# default of WARNING - so naming the wrong logger left that record filtered out, and the test
# passed only when an unrelated earlier test happened to leak an INFO level onto an ancestor.
_SOURCE_LOGGER = _source_mod.logger.name


def _ts(offset_s: float = 0.0) -> str:
    return (datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            + timedelta(seconds=offset_s)).isoformat()


def _write(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, e in enumerate(events):
            rec = {
                "schema_version": 2, "record_type": "span_event",
                "trace_id": e.get("job_id", ""), "span_id": f"s{i:04d}",
                "parent_span_id": None, "seq": i, "ts": e["timestamp"],
                "name": e["event_type"], "kind": "event", "component": "event",
                "attributes": ({"attempt": e["attempt"]}
                               if e.get("attempt") is not None else {}),
                "payload": e.get("payload", {}),
            }
            f.write(json.dumps(rec) + "\n")


def _complete_events(job_id: str) -> list[dict]:
    return [
        {"event_type": "intake_complete", "job_id": job_id, "timestamp": _ts(0),
         "attempt": 0, "payload": {
             "intake_turns": [{"role": "user", "content": "mesh it"}],
             "intake_system_snapshot": "sys", "intake_llm_metadata": [],
             "request_txt": "run cfd", "review_brief_txt": "check mesh",
             "domain": "aero", "max_turns_reached": False,
         }},
        {"event_type": "engine_select_run", "job_id": job_id, "timestamp": _ts(0.5),
         "payload": {"chosen": "cfmesh", "source": "selector"}},
        {"event_type": "builder_attempt", "job_id": job_id, "timestamp": _ts(1),
         "attempt": 1, "payload": {
             "builder_message_histories": [{"role": "user", "content": "build"}],
             "builder_tool_call_histories": [], "builder_system_message_snapshots": "sys",
             "builder_full_responses": "done", "builder_call_metadata": [],
             "builder_pruning_events": [], "builder_tools_definition": [],
         }},
        {"event_type": "executor_run", "job_id": job_id, "timestamp": _ts(2),
         "attempt": 1, "payload": {
             "success": False, "executor_stdouts": "", "executor_stderrs": "err",
             "attempt_log_snapshots": "", "mesh_manifest": {},
         }},
        {"event_type": "classifier_run", "job_id": job_id, "timestamp": _ts(3),
         "attempt": 1, "payload": {
             "section": "MESH", "error_source": "executor_fail", "summary": "bad cell quality",
             "failed_gate": "manifest_valid", "failed_axes": [],
             "builder_mode": "retry", "deterministic": True,
         }},
        {"event_type": "builder_attempt", "job_id": job_id, "timestamp": _ts(4),
         "attempt": 2, "payload": {
             "builder_message_histories": [{"role": "user", "content": "retry"}],
             "builder_tool_call_histories": [], "builder_system_message_snapshots": "sys2",
             "builder_full_responses": "fixed", "builder_call_metadata": [],
             "builder_pruning_events": [], "builder_tools_definition": [],
         }},
        {"event_type": "executor_run", "job_id": job_id, "timestamp": _ts(5),
         "attempt": 2, "payload": {
             "success": True, "executor_stdouts": "ok", "executor_stderrs": "",
             "attempt_log_snapshots": "", "mesh_manifest": {},
         }},
        {"event_type": "reviewer_run", "job_id": job_id, "timestamp": _ts(6),
         "attempt": 2, "payload": {
             "verdict": "PASS", "tool_calls": 2,
             "reviewer_tool_call_histories": [],
             "reviewer_reasoning_chains": ["ok"], "reviewer_full_responses": ["<<PASS>>"],
             "reviewer_system_snapshots": "sys", "reviewer_input_texts": "input",
             "reviewer_review_dirs": "/tmp/r",
         }},
        {"event_type": "final_result_built", "job_id": job_id, "timestamp": _ts(7),
         "attempt": 0, "payload": {
             "final_result": {"schema_version": 1, "status": "succeeded"}, "terminal_message": "Mesh generation completed successfully.", "geometry_source": _GEOMETRY_SOURCE,
             "agent_model_configs": {},
         }},
    ]


_OPERATIONAL = {
    "job_id": "test-job", "user_id": "user-1",
    "session_id": "sess-1", "openfoam_workspace": "/ws/attempt_2",
    "executor_output": "ok", "mesh_manifest": {"cells": 100},
    "builder_noop_count": 0, "builder_mode": "retry",
    "api_failure": "", "domain": "aero",
    "geometry_source": _GEOMETRY_SOURCE, "agent_model_configs": {},
    "request_txt": "run cfd", "review_brief_txt": "check mesh",
    "reviewer_verdict": "<<PASS>>", "retry_count": 2,
    "messages": [], "executor_success": True,
    "outcome_message": "Mesh generated.",
    "reviewer_result": "", "reviewer_feedback": "",
    "reviewer_patch_checks": {}, "reviewer_axis_findings": {},
    "reviewer_rebuild_required": False,
    "reviewer_tool_calls": [], "classifier_result": {},
}



class TestExportSourceErrorMissingLog:

    def test_raises_when_the_authority_holds_nothing_for_the_job(self, capture_authority):
        with pytest.raises(ExportSourceError) as exc_info:
            select_export_source("job-no-records", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert "no capture records" in str(exc_info.value).lower()

    def test_error_message_includes_job_id(self, capture_authority):
        with pytest.raises(ExportSourceError) as exc_info:
            select_export_source("job-missing-123", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert "job-missing-123" in str(exc_info.value)

    def test_the_error_names_the_authority_not_a_file(self, capture_authority):
        with pytest.raises(ExportSourceError) as exc_info:
            select_export_source("job-path-check", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        msg = str(exc_info.value)
        assert "events.jsonl" not in msg and "/" not in msg.replace("job-path-check", "")
        assert "durable authority" in msg

    def test_error_log_emitted_on_missing_file(self, capture_authority, caplog):
        with caplog.at_level(logging.ERROR, logger=_SOURCE_LOGGER):
            with pytest.raises(ExportSourceError):
                select_export_source("job-log-miss", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert any("export_source=none" in r.message for r in caplog.records)

    def test_raises_export_source_error_not_generic(self, capture_authority):
        with pytest.raises(ExportSourceError):
            select_export_source("job-type-check", _OPERATIONAL, owner_id=CAPTURE_OWNER)



class TestExportSourceErrorIncompleteLog:

    def _write_partial(self, capture_authority, job_id):
        seed_events(capture_authority, CAPTURE_OWNER, job_id, [
            {"event_type": "intake_complete", "job_id": job_id,
             "timestamp": _ts(0), "attempt": 0,
             "payload": {"request_txt": "hi"}},
        ])

    def test_raises_when_required_events_missing(self, capture_authority):
        self._write_partial(capture_authority, "job-partial")
        with pytest.raises(ExportSourceError) as exc_info:
            select_export_source("job-partial", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        msg = str(exc_info.value)
        assert "not export-ready" in msg or "gap" in msg or "missing" in msg.lower()

    def test_error_message_mentions_missing_event_types(self, capture_authority):
        self._write_partial(capture_authority, "job-missing-evts")
        with pytest.raises(ExportSourceError) as exc_info:
            select_export_source("job-missing-evts", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        msg = str(exc_info.value)
        assert any(t in msg for t in ("builder_attempt", "executor_run", "reviewer_run", "final_result_built"))

    def test_raises_when_payload_fields_missing(self, capture_authority, tmp_path):
        job_id = "job-empty-payloads"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, [
            {"event_type": "intake_complete",  "job_id": job_id, "timestamp": _ts(0), "attempt": 0, "payload": {}},
            {"event_type": "builder_attempt",  "job_id": job_id, "timestamp": _ts(1), "attempt": 1, "payload": {}},
            {"event_type": "executor_run",     "job_id": job_id, "timestamp": _ts(2), "attempt": 1, "payload": {}},
            {"event_type": "reviewer_run",     "job_id": job_id, "timestamp": _ts(3), "attempt": 1, "payload": {}},
            {"event_type": "final_result_built",       "job_id": job_id, "timestamp": _ts(4), "attempt": 0, "payload": {}},
        ])
        with pytest.raises(ExportSourceError):
            select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)

    def test_error_log_emitted_on_incomplete_log(self, capture_authority, caplog):
        self._write_partial(capture_authority, "job-log-inc")
        with caplog.at_level(logging.ERROR, logger=_SOURCE_LOGGER):
            with pytest.raises(ExportSourceError):
                select_export_source("job-log-inc", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert any("export_source=none" in r.message for r in caplog.records)
        assert any("export_ready=false" in r.message for r in caplog.records)

    def test_raises_when_only_metadata_events(self, capture_authority, tmp_path):
        job_id = "job-metadata-only"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, [
            {"event_type": "intake_complete",  "job_id": job_id, "timestamp": _ts(0), "attempt": 0,
             "payload": {"turns_count": 3, "max_turns_reached": False}},
            {"event_type": "builder_attempt",  "job_id": job_id, "timestamp": _ts(1), "attempt": 1,
             "payload": {"tool_calls": 5, "noop": False}},
            {"event_type": "executor_run",     "job_id": job_id, "timestamp": _ts(2), "attempt": 1,
             "payload": {"success": True, "output_len": 100}},
            {"event_type": "reviewer_run",     "job_id": job_id, "timestamp": _ts(3), "attempt": 1,
             "payload": {"verdict": "PASS", "tool_calls": 2}},
            {"event_type": "final_result_built",       "job_id": job_id, "timestamp": _ts(4), "attempt": 0,
             "payload": {"closing_len": 30}},
        ])
        with pytest.raises(ExportSourceError):
            select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)



class TestSelectExportSourceSuccess:

    def test_returns_events_source_for_complete_log(self, capture_authority, tmp_path):
        job_id = "job-complete"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        state, source = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert source == "events"

    def test_returned_state_is_not_operational_state(self, capture_authority, tmp_path):
        job_id = "job-complete2"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        state, _ = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert state is not _OPERATIONAL

    def test_intake_turns_populated_from_events(self, capture_authority, tmp_path):
        job_id = "job-intake"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        state, _ = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert state["intake_turns"] == [{"role": "user", "content": "mesh it"}]

    def test_request_txt_from_event_payload(self, capture_authority, tmp_path):
        job_id = "job-req"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        state, _ = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert state["request_txt"] == "run cfd"

    def test_reviewer_verdict_reconstructed(self, capture_authority, tmp_path):
        job_id = "job-verdict"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        state, _ = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert state["reviewer_verdict"] == "PASS"

    def test_operational_fields_merged_from_operational_state(self, capture_authority, tmp_path):
        job_id = "job-opfields"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        op = dict(_OPERATIONAL, job_id=job_id, user_id="user-merge",
                  openfoam_workspace="/ws/custom", executor_output="custom-output")
        state, _ = select_export_source(job_id, op, owner_id=CAPTURE_OWNER)
        assert state["user_id"] == "user-merge"
        assert state["openfoam_workspace"] == "/ws/custom"
        assert state["executor_output"] == "custom-output"

    def test_metric_log_emitted_on_success(self, capture_authority, tmp_path, caplog):
        job_id = "job-log-ok"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))
        with caplog.at_level(logging.INFO, logger=_SOURCE_LOGGER):
            select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        msgs = " ".join(r.message for r in caplog.records)
        assert "export_source=event_log" in msgs
        assert "export_ready=true" in msgs
        assert "missing_fields=0" in msgs



class TestExportTaskErrorPropagation:

    def _mock_redis(self, n: int = 1) -> MagicMock:
        client = MagicMock()
        client.incr.return_value = n
        mod = MagicMock()
        mod.from_url.return_value = client
        return mod

    def test_export_raises_export_source_error_when_no_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
        monkeypatch.setattr(envcfg, "PROMPTS_DIR", tmp_path)

        with patch.dict(sys.modules, {"redis": self._mock_redis()}):
            from meshpipeline.application.maintenance.export import _export
            with pytest.raises(ExportSourceError):
                _export("job-no-evts", _OPERATIONAL)

    def test_export_returns_ok_when_events_complete(self, capture_authority, tmp_path, monkeypatch):
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
        monkeypatch.setattr(envcfg, "PROMPTS_DIR", tmp_path)

        job_id = "job-evts-ok"
        seed_events(capture_authority, CAPTURE_OWNER, job_id, _complete_events(job_id))

        with patch.dict(sys.modules, {"redis": self._mock_redis(42)}):
            from meshpipeline.application.maintenance.export import _export
            result = _export(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)

        assert result["status"] == "ok"
        assert result["job_id"] == job_id



class TestExportSourceAuthorityContract:

    def _complete(self, authority, job_id, owner=CAPTURE_OWNER):
        seed_events(authority, owner, job_id, _complete_events(job_id))

    def test_a_foreign_tenant_cannot_select_another_owners_capture(self, capture_authority):
        self._complete(capture_authority, "job-tenant")
        with pytest.raises(ExportSourceError):
            select_export_source("job-tenant", _OPERATIONAL, owner_id=FOREIGN_OWNER)
        # and the rightful owner is unaffected
        state, src = select_export_source("job-tenant", _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert src and state

    def test_an_export_without_a_tenant_is_refused_rather_than_guessed(self, capture_authority):
        self._complete(capture_authority, "job-anon")
        with pytest.raises(ExportSourceError) as exc:
            select_export_source("job-anon", _OPERATIONAL, owner_id="")
        assert "tenant" in str(exc.value).lower()

    def test_a_conflicted_operation_is_excluded_from_the_selected_source(self, capture_authority):
        job_id = "job-conflicted"
        self._complete(capture_authority, job_id)
        before = len(capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id))
        seed_conflict(capture_authority, CAPTURE_OWNER, job_id, op_key="extra",
                      name="builder_attempt")

        after = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)
        assert len(after) == before, "a disputed operation reached the export source"
        assert capture_authority.conflicted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)

    def test_the_selected_source_is_ordered_by_the_durable_sequence(self, capture_authority):
        job_id = "job-order"
        self._complete(capture_authority, job_id)
        rows = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)
        assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)

    def test_two_exports_of_unchanged_state_are_identical(self, capture_authority):
        job_id = "job-determinism"
        self._complete(capture_authority, job_id)
        first = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        second = select_export_source(job_id, _OPERATIONAL, owner_id=CAPTURE_OWNER)
        assert first == second

    def test_an_unavailable_repository_fails_the_export_rather_than_reading_a_file(
            self, capture_authority, monkeypatch, tmp_path):
        import meshpipeline.persistence.repositories.capture_repository as cap

        def _down(**_):
            raise ConnectionError("capture authority unreachable")

        self._complete(capture_authority, "job-down")
        monkeypatch.setattr(cap, "trusted_operations", _down)
        # a stale local corpus file exists and must not be consulted
        stale = tmp_path / "job-down" / "events.jsonl"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"record_type": "span_event", "name": "stale", "payload": {}}\n')
        monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))

        with pytest.raises(ExportSourceError):
            select_export_source("job-down", _OPERATIONAL, owner_id=CAPTURE_OWNER)

    def test_a_malformed_durable_record_cannot_become_trusted_export_data(self, capture_authority):
        from meshpipeline.capture.events import EventLog
        job_id = "job-malformed"
        self._complete(capture_authority, job_id)
        rows = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)
        good = len(EventLog(job_id, owner_id=CAPTURE_OWNER).load())
        assert good == len(rows) > 0

        broken = EventLog.from_records(job_id, [
            {"record_type": "span_event", "name": "x", "payload": {}},          # no ts
            {"record_type": "span_event", "name": "y", "ts": "not-a-time", "payload": {}},
        ])
        assert broken.load() == []
