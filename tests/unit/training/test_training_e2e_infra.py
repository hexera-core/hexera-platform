# Responsibility: Verify emitted events become durable records, and both workers share one corpus mount.
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from tests.capture_authority import CAPTURE_OWNER
from tests.product_modes import set_modes

import meshpipeline.runtime.startup as startupcfg
import meshpipeline.settings.env as envcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.capture.events import EventLog

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
REPO_ROOT = Path(__file__).parent.parent.parent.parent

for _mod in ["langfuse", "langfuse.decorators"]:
    sys.modules.setdefault(_mod, MagicMock())

_mock_redis_client = MagicMock()
_mock_redis_client.incr.return_value = 1
_mock_redis_mod = MagicMock()
_mock_redis_mod.from_url.return_value = _mock_redis_client
sys.modules.setdefault("redis", _mock_redis_mod)



class TestAPIDoesNotWriteTrainingLogger:

    def _make_intake_state(self, request_txt: str = "") -> dict:
        return {
            "job_id":         str(uuid.uuid4()),
            "user_id":        "test-user",
            "session_id":     str(uuid.uuid4()),
            "geometry_source": _GEOMETRY_SOURCE,
            "messages":       [],
            "request_txt":    request_txt,
            "review_brief_txt": "",
            "domain": "",
        }

    def test_idempotency_guard_returns_empty(self):
        import asyncio

        from meshpipeline.agents.intake.agent import node_intake

        state = self._make_intake_state(request_txt="already done")
        result = asyncio.run(node_intake(state))
        assert result == {}, "Idempotency guard must return empty dict"

    def test_intake_turn_returns_training_event_not_file(self, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        import asyncio

        from meshpipeline.agents.intake.agent import node_intake
        from meshpipeline.contracts.model_inference import ModelRoundResult
        mock_response = ModelRoundResult(assistant_text="What simulation type?",
                                         finish_reason="stop")

        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)), \
             patch("meshpipeline.adapters.model_inference.router.call_intake_model", return_value=mock_response):
            state = self._make_intake_state()
            result = asyncio.run(node_intake(state))

        assert "_intake_training_event" in result
        ev = result["_intake_training_event"]
        assert ev["type"] == "intake_turn"
        assert "turn" in ev["payload"]
        assert "finish_reason" in ev["payload"]

        assert not any(tmp_path.rglob("events.jsonl")), (
            "node_intake must not write to CORPUS_DIR - "
            "worker is the sole writer"
        )

    def test_intake_complete_returns_training_event_not_file(self, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        import asyncio
        import json as _json

        from meshpipeline.agents.intake.agent import node_intake

        submit_tool_call = MagicMock()
        submit_tool_call.id = "tc-001"
        submit_tool_call.function.name = "submit_requirements"
        submit_tool_call.function.arguments = _json.dumps({
            "domain":          "external aerodynamics CFD",
            "request_txt":     (
                "NACA 0012 airfoil at Mach 0.3, chord 1m, angle of attack 5 degrees. "
                "Standard-resolution analysis for design iteration. External flow "
                "domain with H-H box topology, refined near the leading edge."
            ),
            "review_brief_txt": (
                "Domain must be 20 chords upstream, wake 50 chords downstream. "
                "Wall patches should resolve y+ in the 30-300 range. Cell count "
                "target ~120k. Smooth size transitions between near-wall and wake."
            ),
            "mesh_engine":     "cfmesh",  # the 2D-capable engine (native cartesian2DMesh);
            "mesh_fidelity": "standard", "engine_source":   "suggested_confirmed",  # snappy is 3D-only upstream
            "engine_params":   {},   # cfmesh declares none; topology derives from purpose
            "patches": [
                {"name": "airfoil",       "type": "wall"},
                {"name": "farfield",      "type": "farfield"},
                {"name": "frontAndBack",  "type": "empty"},
            ],
            "dimensionality": "2D",
            "purpose": "external_cfd",
            "input_kind": "body-surface",
        })

        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest

        # Built lazily: the test finalizes submit_tool_call.function.arguments (with the preview
        # token) only after seeding the gate below, and ModelRoundResult is frozen.
        def _tool_round():
            return ModelRoundResult(
                tool_calls=(ToolCallRequest(id=submit_tool_call.id,
                                            name=submit_tool_call.function.name,
                                            arguments=submit_tool_call.function.arguments),),
                finish_reason="tool_calls")

        def _text_round():
            return ModelRoundResult(assistant_text="Requirements submitted. Proceeding.",
                                    finish_reason="stop")

        call_responses = [_tool_round, _text_round]

        async def _fake_call(**kwargs):
            return call_responses.pop(0)()

        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)), \
             patch("meshpipeline.adapters.model_inference.router.call_intake_model", side_effect=_fake_call):
            state = self._make_intake_state()
            # authorize the submission with a matching single-engine supported preview token
            import meshpipeline.agents.intake.admission_token as _at
            import meshpipeline.agents.intake.engine_selection as _es
            _a = _json.loads(submit_tool_call.function.arguments)
            _sel = _es.select_from_structured_input(
                _a["mesh_engine"], session_id=str(state.get("session_id", "")),
                owner_id=str(state.get("user_id", "")),
                revision=_at.revision_of(state.get("messages", [])))
            _tok = _at.issue(session_id=str(state.get("session_id", "")),
                             owner_id=str(state.get("user_id", "")),
                             revision=_at.revision_of(state.get("messages", [])),
                             canonical=_at.canonical_payload(_a["mesh_engine"], _a["purpose"],
                                _a["input_kind"], _a["dimensionality"], _a["patches"], _a["engine_params"]),
                             verdict="supported", mode=_at.SELECTED, selection_id=_sel["id"])
            state["intake_gate"] = {"selection": _sel, "admission": _tok}
            _a["preview_token"] = _tok["token"]
            submit_tool_call.function.arguments = _json.dumps(_a)
            result = asyncio.run(node_intake(state))

        assert "_intake_training_event" in result
        ev = result["_intake_training_event"]
        assert ev["type"] == "intake_complete"
        payload = ev["payload"]
        assert payload["domain"] == "external aerodynamics CFD"
        assert "intake_turns" in payload
        assert "intake_system_snapshot" in payload
        assert "request_txt" in payload
        assert "review_brief_txt" in payload

        assert not any(tmp_path.rglob("events.jsonl")), (
            "node_intake must not write to CORPUS_DIR"
        )

class TestWorkerIntakeEmission:

    def _make_intake_events(self) -> list:
        return [
            {
                "type": "intake_turn",
                "payload": {
                    "turn": 1,
                    "finish_reason": "stop",
                    "max_turns_reached": False,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                },
            },
            {
                "type": "intake_turn",
                "payload": {
                    "turn": 2,
                    "finish_reason": "stop",
                    "max_turns_reached": False,
                    "usage": {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
                },
            },
            {
                "type": "intake_complete",
                "payload": {
                    "domain":               "external CFD",
                    "max_turns_reached":    False,
                    "finish_reason":        "stop",
                    "mach":                 0.3,
                    "aoa":                  5.0,
                    "turbulence":           "kOmegaSST",
                    "intake_turns":         [{"role": "user", "content": "NACA 0012"}],
                    "intake_system_snapshot": "You are an intake agent.",
                    "intake_llm_metadata":  [{"turn": 3, "completed": True}],
                    "request_txt":          "Mesh a NACA 0012 wing at Mach 0.3.",
                    "review_brief_txt":     "Domain must be 20 chords upstream.",
                },
            },
        ]

    def test_emit_writes_all_events(self, capture_authority, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.pipeline_run import _emit_intake_events

        job_id = str(uuid.uuid4())
        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)):
            _emit_intake_events(job_id, self._make_intake_events())

        lines = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)
        assert lines, "intake events must reach the durable authority"
        types = [l["name"] for l in lines]
        assert types == ["intake_turn", "intake_turn", "intake_complete"], (
            f"Expected 2×intake_turn then intake_complete, got: {types}"
        )

    def test_emit_intake_complete_payload_preserved(self, capture_authority, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.pipeline_run import _emit_intake_events

        job_id = str(uuid.uuid4())
        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)):
            _emit_intake_events(job_id, self._make_intake_events())

        lines = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)
        complete = next(l for l in lines if l["name"] == "intake_complete")
        p = complete["payload"]
        assert p["request_txt"] == "Mesh a NACA 0012 wing at Mach 0.3."
        assert p["domain"] == "external CFD"
        assert "intake_turns" in p
        assert "intake_system_snapshot" in p

    def test_emit_passes_through_all_event_types_with_non_empty_type(self, capture_authority, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.pipeline_run import _emit_intake_events

        job_id = str(uuid.uuid4())
        events = [
            {"type": "",                  "payload": {"skipped": True}},
            {"type": "some_unknown_event", "payload": {"x": 1}},
            {"type": "intake_complete", "payload": {
                "domain": "CFD", "intake_turns": [], "intake_system_snapshot": "",
                "intake_llm_metadata": [], "request_txt": "req", "review_brief_txt": "brief",
                "max_turns_reached": False, "finish_reason": "stop",
            }},
        ]
        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)):
            _emit_intake_events(job_id, events)

        lines = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)
        types = [l["name"] for l in lines]
        assert types == ["some_unknown_event", "intake_complete"]
        assert not any(l["payload"].get("skipped") for l in lines)

    def test_emit_empty_list_creates_no_file(self, capture_authority, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.pipeline_run import _emit_intake_events

        job_id = str(uuid.uuid4())
        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)):
            _emit_intake_events(job_id, [])

        assert capture_authority.trusted_operations(
            owner_id=CAPTURE_OWNER, job_id=job_id) == [], (
            "an empty intake list must record no capture operations")



class TestFullEventLogExportReady:

    def _write_pipeline_events(self, job_id: str, base_dir: Path) -> None:
        from meshpipeline.capture.logger import TrainingLogger
        tlogger = TrainingLogger(job_id)
        tlogger.log("engine_select_run", op_id="engine_select_run:1", payload={"chosen": "cfmesh", "source": "selector"})
        tlogger.log("builder_attempt", op_id="builder_attempt:1", payload={
            "mode":                             "initial",
            "executor_success":                 True,
            "tool_calls":                       5,
            "response_len":                     200,
            "builder_message_histories":        [{"role": "user", "content": "build it"}],
            "builder_tool_call_histories":      [{"name": "write_file"}],
            "builder_system_message_snapshots": "System prompt.",
            "builder_full_responses":           "Final response.",
            "builder_call_metadata":            [{"finish_reason": "tool_calls"}],
            "builder_pruning_events":           [],
            "builder_tools_definition":         [{"name": "write_file"}],
        }, attempt=0)
        tlogger.log("executor_run", op_id="executor_run:1", payload={
            "success":               True,
            "output_len":            500,
            "workspace":             "/srv/workspaces/test",
            "executor_stdouts":      "OpenFOAM completed successfully.",
            "executor_stderrs":      "",
            "attempt_log_snapshots": "Attempt 1: success.",
            "mesh_manifest":         {"cells": 100000},
        }, attempt=0)
        tlogger.log("classifier_run", op_id="classifier_run:1", payload={
            "section":       "MESH",
            "error_source":  "executor_fail",
            "summary":       "bad cell quality",
            "failed_gate":   "manifest_valid",
            "failed_axes":   [],
            "builder_mode":  "retry",
            "deterministic": True,
        }, attempt=0)
        tlogger.log("reviewer_run", op_id="reviewer_run:1", payload={
            "verdict":                      "PASS",
            "tool_calls":                   3,
            "tool_limit_reached":           False,
            "reviewer_tool_call_histories": [{"name": "check_mesh"}],
            "reviewer_reasoning_chains":    ["Mesh quality is good."],
            "reviewer_full_responses":      ["<<PASS>>"],
            "reviewer_system_snapshots":    "Reviewer system.",
            "reviewer_input_texts":         "Review this mesh.",
            "reviewer_review_dirs":         "/srv/workspaces/test/review",
        }, attempt=0)
        tlogger.log("final_result_built", op_id="final_result_built:1", payload={
            "outcome_message":        "Your mesh is ready.",
            "final_result": {"schema_version": 1, "status": "succeeded"}, "terminal_message": "Mesh generation completed successfully.",
            "geometry_source":         _GEOMETRY_SOURCE,
            "mach":                   0.3,
            "agent_model_configs":    {"builder": {"model": "kimi-k2"}},
        }, attempt=0)

    def test_worker_only_log_is_export_ready(self, capture_authority, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.pipeline_run import _emit_intake_events

        job_id = str(uuid.uuid4())

        intake_events = [
            {"type": "intake_complete", "payload": {
                "domain":               "external CFD",
                "max_turns_reached":    False,
                "finish_reason":        "stop",
                "mach":                 0.3, "aoa": 5.0, "turbulence": "kOmegaSST",
                "intake_turns":         [{"role": "user", "content": "NACA 0012"}],
                "intake_system_snapshot": "You are an intake agent.",
                "intake_llm_metadata":  [{"turn": 1, "completed": True}],
                "request_txt":          "Mesh a NACA 0012 wing.",
                "review_brief_txt":     "Domain 20 chords upstream.",
            }},
        ]

        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)):
            _emit_intake_events(job_id, intake_events)
            self._write_pipeline_events(job_id, tmp_path)

        report = EventLog(job_id, owner_id=CAPTURE_OWNER).validate_completeness()
        assert report.is_export_ready, (
            f"Event log must be export-ready without API filesystem access.\n"
            f"Missing types: {report.missing_event_types}\n"
            f"Gaps: {report.gaps}"
        )

    def test_api_filesystem_absence_does_not_block_export(self, capture_authority, tmp_path, monkeypatch):
        set_modes(monkeypatch, collection=True)
        from meshpipeline.application.pipeline_run import _emit_intake_events

        job_id     = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        intake_events = [
            {"type": "intake_complete", "payload": {
                "domain": "FEA structural", "max_turns_reached": False,
                "finish_reason": "stop", "mach": None, "aoa": None, "turbulence": None,
                "intake_turns": [], "intake_system_snapshot": "system",
                "intake_llm_metadata": [], "request_txt": "Analyse the bracket.",
                "review_brief_txt": "Ensure no high-stress zones.",
            }},
        ]

        with patch("meshpipeline.settings.runtime.JOBS_DIR", tmp_path, create=True), patch("meshpipeline.settings.runtime.CORPUS_DIR", str(tmp_path)):
            _emit_intake_events(job_id, intake_events)
            self._write_pipeline_events(job_id, tmp_path)

        assert not (tmp_path / session_id).exists()

        report = EventLog(job_id, owner_id=CAPTURE_OWNER).validate_completeness()
        assert report.is_export_ready, (
            f"Export must not depend on session_id directory.\nGaps: {report.gaps}"
        )



class TestComposeVolumeConfig:

    @staticmethod
    def _dest(v) -> str:
        if isinstance(v, str):
            return v.split(":")[-1].rstrip("/")
        return v.get("target", "").rstrip("/")

    @staticmethod
    def _source(v) -> str | None:
        if isinstance(v, str):
            parts = v.split(":")
            return parts[0] if len(parts) >= 2 else None
        return v.get("source")

    def test_worker_service_has_corpus_mount(self):
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            compose = yaml.safe_load(f)

        worker_volumes = compose["services"]["worker"].get("volumes", [])
        dests = [self._dest(v) for v in worker_volumes]
        assert "/srv/data" in dests, (
            f"worker service must mount /data/corpus; found: {dests}"
        )

    def test_worker_utility_service_has_corpus_mount(self):
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            compose = yaml.safe_load(f)

        wu_volumes = compose["services"]["worker-utility"].get("volumes", [])
        dests = [self._dest(v) for v in wu_volumes]
        assert "/srv/data" in dests, (
            f"worker-utility service must mount /data/corpus; found: {dests}"
        )

    def test_worker_and_worker_utility_share_same_training_source(self):
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            compose = yaml.safe_load(f)

        def _training_src(volumes):
            for v in volumes:
                if self._dest(v) == "/srv/data":
                    return self._source(v)
            return None

        w_src  = _training_src(compose["services"]["worker"].get("volumes", []))
        wu_src = _training_src(compose["services"]["worker-utility"].get("volumes", []))

        assert w_src  is not None, "worker has no /data/corpus mount"
        assert wu_src is not None, "worker-utility has no /data/corpus mount"
        assert w_src == wu_src, (
            f"worker ({w_src!r}) and worker-utility ({wu_src!r}) mount different "
            "/data/corpus sources"
        )



class TestTrainingDirValidation:

    def test_validate_warns_on_unwritable_dir(self, tmp_path, monkeypatch):
        import warnings

        set_modes(monkeypatch, collection=True)
        unwritable = tmp_path / "no_write"
        unwritable.mkdir()
        unwritable.chmod(0o444)

        try:
            with patch.object(rtcfg, "CORPUS_DIR", str(unwritable)), \
                 patch.object(envcfg, "PROMPTS_DIR", tmp_path / "noprompts"), \
                 patch.object(rtcfg, "WORKSPACE_BASE", tmp_path), \
                 patch.object(rtcfg, "JOBS_DIR", tmp_path, create=True):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    startupcfg.validate()
                msgs = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
                assert any("CORPUS_DIR" in m for m in msgs), (
                    f"Expected RuntimeWarning about CORPUS_DIR, got: {msgs}"
                )
        finally:
            unwritable.chmod(0o755)

    def test_validate_logs_path_on_success(self, tmp_path, caplog, monkeypatch):
        import logging
        import warnings

        set_modes(monkeypatch, collection=True)
        training_dir = tmp_path / "training"

        with patch.object(rtcfg, "CORPUS_DIR", str(training_dir)), \
             patch.object(envcfg, "PROMPTS_DIR", tmp_path / "noprompts"), \
             patch.object(rtcfg, "WORKSPACE_BASE", tmp_path), \
             patch.object(rtcfg, "JOBS_DIR", tmp_path, create=True):
            with caplog.at_level(logging.INFO), warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                startupcfg.validate()

        assert training_dir.exists(), "validate() should create the dir"
        assert any("corpus dir" in r.message for r in caplog.records), (
            "Expected INFO log naming the corpus dir"
        )

    def test_validate_provisions_no_corpus_when_collection_is_off(self, tmp_path, monkeypatch):
        import warnings

        set_modes(monkeypatch, collection=False)
        training_dir = tmp_path / "training"
        with patch.object(rtcfg, "CORPUS_DIR", str(training_dir)), \
             patch.object(envcfg, "PROMPTS_DIR", tmp_path / "noprompts"), \
             patch.object(rtcfg, "WORKSPACE_BASE", tmp_path), \
             patch.object(rtcfg, "JOBS_DIR", tmp_path, create=True):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                startupcfg.validate()
        assert not training_dir.exists(), "a non-collecting deployment provisioned corpus storage"

    def test_summary_includes_corpus_dir(self):
        s = startupcfg.summary()
        assert "Corpus dir" in s
