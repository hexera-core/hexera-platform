# Responsibility: Verify the fallback backend execution id is unique per OS process, so forked pool children never share one.
from __future__ import annotations

import os
import sys

import pytest

import meshpipeline.application.pipeline_run as pr
import meshpipeline.settings.providers as provcfg


@pytest.fixture(autouse=True)
def _no_explicit_execution_id(monkeypatch):
    monkeypatch.setattr(provcfg, "PIPELINE_EXECUTION_ID", "", raising=False)
    monkeypatch.setattr(provcfg, "PIPELINE_BACKEND", "celery", raising=False)


def test_same_process_redelivery_shares_the_id():
    assert pr._backend_execution_id("job-A") == pr._backend_execution_id("job-B")


def test_explicit_execution_id_still_wins(monkeypatch):
    monkeypatch.setattr(provcfg, "PIPELINE_EXECUTION_ID", "cloudrun-exec-7", raising=False)
    assert pr._backend_execution_id("job-A") == ("celery", "cloudrun-exec-7")


def test_a_new_pid_is_a_new_execution(monkeypatch):
    first = pr._backend_execution_id("job-A")[1]
    real_pid = os.getpid()
    monkeypatch.setattr(pr.os, "getpid", lambda: real_pid + 1)
    second = pr._backend_execution_id("job-A")[1]
    assert second != first
    assert pr._backend_execution_id("job-A")[1] == second


@pytest.mark.skipif(not hasattr(os, "fork") or sys.platform == "win32", reason="needs os.fork")
def test_forked_children_after_parent_import_get_distinct_ids():
    # What Celery prefork does: the parent imports (and may use) the module, then forks children.
    parent = pr._backend_execution_id("job-P")[1]

    def child_id() -> str:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            os.close(r)
            os.write(w, pr._backend_execution_id("job-C")[1].encode())
            os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)
        with os.fdopen(r, "rb") as fh:
            return fh.read().decode()

    a, b = child_id(), child_id()
    assert a and b
    assert len({parent, a, b}) == 3
