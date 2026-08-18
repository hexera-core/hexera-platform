# Responsibility: Verify post-terminal recording exports allow-listed fields only and never affects the verdict.
from __future__ import annotations

import uuid

import pytest
from tests.product_modes import set_modes

from meshpipeline.application import post_terminal as pt

JOB = str(uuid.uuid4())


class _Log:
    def __init__(self): self.warnings = []; self.errors = []; self.infos = []
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): self.errors.append(a)
    def info(self, *a, **k): self.infos.append(a)


# the export allow-list

def test_the_export_carries_only_allow_listed_fields(monkeypatch):
    sent: list = []
    import meshpipeline.contracts.training_export as te
    set_modes(monkeypatch, collection=True)
    monkeypatch.setattr(te, "enqueue_export", lambda j, s, **k: sent.append(s))

    state = {"engine": "cfmesh", "retry_count": 2,
             "worker_token": "SECRET-TOKEN", "internal_lease": {"row": 1}}
    pt.enqueue_conversation_export(JOB, state, created_at=None, ended_at=None, jlog=_Log())
    assert sent, "nothing was enqueued"
    assert "worker_token" not in sent[0], "a key nobody allow-listed was exported"
    assert "internal_lease" not in sent[0]
    assert sent[0]["engine"] == "cfmesh" and sent[0]["retry_count"] == 2


def test_the_allow_list_is_a_frozen_set_not_a_redaction_pass():
    assert isinstance(pt.EXPORT_FIELDS, frozenset)
    assert "worker_token" not in pt.EXPORT_FIELDS


def test_the_export_is_skipped_when_collection_is_disabled(monkeypatch):
    called: list = []
    import meshpipeline.contracts.training_export as te
    set_modes(monkeypatch, collection=False)
    monkeypatch.setattr(te, "enqueue_export", lambda *a, **k: called.append(1))
    pt.enqueue_conversation_export(JOB, {"engine": "x"}, created_at=None, ended_at=None, jlog=_Log())
    assert not called


def test_a_failing_export_never_raises(monkeypatch):
    import meshpipeline.contracts.training_export as te
    set_modes(monkeypatch, collection=True)

    def _boom(*a, **k): raise RuntimeError("queue down")
    monkeypatch.setattr(te, "enqueue_export", _boom)
    log = _Log()
    pt.enqueue_conversation_export(JOB, {"engine": "x"}, created_at=None, ended_at=None, jlog=log)
    assert log.errors, "a failed export was silent"


def test_unserializable_values_are_coerced(monkeypatch):
    sent: list = []
    import meshpipeline.contracts.training_export as te
    set_modes(monkeypatch, collection=True)
    monkeypatch.setattr(te, "enqueue_export", lambda j, s, **k: sent.append(s))

    class _Model:
        def model_dump(self): return {"ok": True}

    pt.enqueue_conversation_export(JOB, {"mesh_manifest": _Model()},
                                   created_at=None, ended_at=None, jlog=_Log())
    assert sent[0]["mesh_manifest"] == {"ok": True}


# terminal capture

def test_the_terminal_record_is_keyed_per_job_and_generation(monkeypatch):
    logged: list = []
    import meshpipeline.capture.logger as cl

    class _TL:
        def __init__(self, job_id): self.job_id = job_id
        def log(self, event, *, op_id, payload): logged.append((event, op_id, payload))
    monkeypatch.setattr(cl, "TrainingLogger", _TL)

    pt.capture_terminal_record(JOB, generation=3, final_result={"status": "succeeded"},
                               terminal_message="done", geometry_source={"id": "g"},
                               agent_model_configs={}, jlog=_Log())
    assert logged[0][0] == "final_result_built"
    assert logged[0][1] == "terminal:g3", (
        "the terminal record is not keyed per generation - a takeover would add a second "
        "training example of the same outcome")


def test_a_failing_capture_never_affects_the_verdict(monkeypatch):
    import meshpipeline.capture.logger as cl

    class _Boom:
        def __init__(self, job_id): raise RuntimeError("capture down")
    monkeypatch.setattr(cl, "TrainingLogger", _Boom)
    log = _Log()
    pt.capture_terminal_record(JOB, generation=1, final_result={}, terminal_message="",
                               geometry_source=None, agent_model_configs={}, jlog=log)
    assert log.warnings, "a failed capture was silent"


# viewer preview

def test_the_preview_is_copied_into_the_always_kept_job_record(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "mesh.msh").write_text("MESH")
    monkeypatch.setattr(rtcfg, "JOBS_DIR", str(tmp_path / "jobs"))
    pt.copy_viewer_preview(JOB, str(ws), jlog=_Log())
    assert (tmp_path / "jobs" / JOB / "preview.msh").read_text() == "MESH"


def test_only_the_first_candidate_is_copied(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "mesh.msh").write_text("M"); (ws / "mesh.stl").write_text("S")
    monkeypatch.setattr(rtcfg, "JOBS_DIR", str(tmp_path / "jobs"))
    pt.copy_viewer_preview(JOB, str(ws), jlog=_Log())
    made = sorted(p.name for p in (tmp_path / "jobs" / JOB).iterdir())
    assert made == ["preview.msh"], made


@pytest.mark.parametrize("workspace", ["", "/nonexistent/workspace"])
def test_a_missing_workspace_is_not_an_error(workspace, monkeypatch, tmp_path):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "JOBS_DIR", str(tmp_path / "jobs"))
    pt.copy_viewer_preview(JOB, workspace, jlog=_Log())      # must not raise


# mutation guard

def test_the_orchestrator_no_longer_owns_these_records():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "_EXPORT_FIELDS" not in src, "the run owns the export allow-list again"
    assert "final_result_built" not in src, "the run writes the terminal capture again"
    assert "shutil.copy2" not in src, "the run copies the preview again"
    # (the run still writes its own `dispute_context` capture inside the dispute-seeding block -
    # a different record, extracted with that block, not this one)
    for delegated in ("capture_terminal_record", "copy_viewer_preview",
                      "enqueue_conversation_export"):
        assert delegated in src, f"the run no longer delegates {delegated}"
