# Responsibility: Verify one boolean switch governs collection, defaults off, and refuses a malformed value.
from __future__ import annotations

from pathlib import Path

import pytest
from tests.capture_authority import CAPTURE_OWNER

import meshpipeline.settings.runtime as rtcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
REPO_DIR = Path(__file__).parent.parent.parent.parent

from tests.product_modes import set_modes

from meshpipeline.settings.env import ConfigurationError, bool_env

# strict parsing

def test_bool_env_accepts_the_documented_true_and_false_spellings(monkeypatch):
    for v in ("true", "1", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("DATA_COLLECTION_ENABLED", v)
        assert bool_env("DATA_COLLECTION_ENABLED", "false") is True, v
    for v in ("false", "0", "no", "off", "False", "OFF"):
        monkeypatch.setenv("DATA_COLLECTION_ENABLED", v)
        assert bool_env("DATA_COLLECTION_ENABLED", "false") is False, v


def test_bool_env_defaults_false_when_unset(monkeypatch):
    monkeypatch.delenv("DATA_COLLECTION_ENABLED", raising=False)
    assert bool_env("DATA_COLLECTION_ENABLED", "false") is False


def test_malformed_value_fails_fast_with_a_clear_error(monkeypatch):
    monkeypatch.setenv("DATA_COLLECTION_ENABLED", "maybe")
    with pytest.raises(ConfigurationError) as exc:
        bool_env("DATA_COLLECTION_ENABLED", "false")
    assert "DATA_COLLECTION_ENABLED" in str(exc.value)
    assert "maybe" in str(exc.value)


# disabled: representative capture calls write NOTHING

def test_disabled_collection_writes_nothing_optional(capture_authority, monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    set_modes(monkeypatch, collection=False)
    from meshpipeline.capture.logger import TrainingLogger

    TrainingLogger("job-x").log("intake_complete", {"request_txt": "hello"}, op_id="intake:1")

    assert capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id="job-x") == []
    assert capture_authority.conflicted_operations(owner_id=CAPTURE_OWNER, job_id="job-x") == []
    assert not (tmp_path / "job-x").exists()


def test_a_disabled_deployment_cannot_be_made_to_conflict_either(capture_authority, monkeypatch,
                                                                 tmp_path):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False)
    set_modes(monkeypatch, collection=False)
    from meshpipeline.capture.logger import TrainingLogger
    TrainingLogger("job-z").log("executor_run", {"success": True}, op_id="e:1")
    TrainingLogger("job-z").log("executor_run", {"success": False}, op_id="e:1")

    assert capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id="job-z") == []
    assert capture_authority.conflicted_operations(owner_id=CAPTURE_OWNER, job_id="job-z") == []


def test_disabled_export_task_reports_disabled_and_corpus_stays_empty(monkeypatch, tmp_path):
    set_modes(monkeypatch, collection=False)
    corpus = tmp_path / "corpus"; corpus.mkdir()
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(corpus))
    from meshpipeline.application.maintenance.export import export_conversation_sample
    out = export_conversation_sample("job-x", {"engine": "cfmesh"})
    assert out["status"] == "disabled"
    assert list(corpus.iterdir()) == []   # NOTHING exported to the dataset


# enabled: the existing record shape is unchanged

def test_enabled_stores_the_capture_row(capture_authority, monkeypatch, tmp_path):
    set_modes(monkeypatch, collection=True)
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    from meshpipeline.capture.logger import TrainingLogger

    TrainingLogger("job-y").log("intake_complete", {"request_txt": "hello"}, op_id="intake:1")
    (row,) = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id="job-y")
    assert row["record_type"] == "span_event" and row["name"] == "intake_complete"
    assert row["payload"] == {"request_txt": "hello"}


def test_generated_env_documents_the_switch():
    # WHICH way it ships is a product decision. That the template states it, and states the same
    # value the catalogue holds, is the invariant - an operator must never have to guess.
    from meshpipeline.settings import inventory
    declared = inventory.default_of("DATA_COLLECTION_ENABLED")
    assert declared in ("true", "false"), declared
    assert f"DATA_COLLECTION_ENABLED={declared}" in inventory.render_env()
