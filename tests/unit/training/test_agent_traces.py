# Responsibility: Verify a capture record is stored durably, redacted, and refused without an occurrence identity.
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.capture_authority import CAPTURE_OWNER

REPO_DIR = Path(__file__).parent.parent.parent.parent

from tests.product_modes import set_modes

from meshpipeline.capture import trace  # noqa: E402


@pytest.fixture()
def collecting(monkeypatch):
    set_modes(monkeypatch, collection=True)


def _durable(authority, job_id: str) -> list[dict]:
    return authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)


def test_secrets_are_redacted_before_they_are_stored(capture_authority, collecting, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    trace.add_event("j-sec", "x", {"text": "key is supersecretvalue123 ok"},
                    attributes={"op_id": "sec:1"})

    durable = json.dumps(_durable(capture_authority, "j-sec"), default=str)
    assert "supersecretvalue123" not in durable, "a secret reached durable storage"
    assert "[REDACTED:MY_API_KEY]" in durable


def test_traininglogger_records_content_durably(capture_authority, collecting):
    from meshpipeline.capture.logger import TrainingLogger
    TrainingLogger("j-lg").log("executor_run", {"success": True}, attempt=2, op_id="exec:1")

    (row,) = _durable(capture_authority, "j-lg")
    assert row["name"] == "executor_run"
    assert row["attempt"] == 2
    assert row["payload"] == {"success": True}


def test_a_record_without_an_occurrence_identity_is_not_stored(capture_authority, collecting):
    trace.add_event("j-anon", "x", {"a": 1})
    assert _durable(capture_authority, "j-anon") == []


def test_data_tree_is_gitignored():
    gi = (REPO_DIR / ".gitignore").read_text()
    assert re.search(r"^data/$", gi, re.M), "data/ (jobs+corpus) must stay untracked"
