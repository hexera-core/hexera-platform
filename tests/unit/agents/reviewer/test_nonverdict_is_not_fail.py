# Responsibility: Verify a review that reached no verdict is recorded as no verdict, never as "FAIL".
# Boundaries: the review's saved verdict.json and the viewer payload that reads it back; the job
# status's own rule (the durable record wins) is pinned in tests/unit/api/test_api_simulation.py.
from __future__ import annotations

import json

from meshpipeline.agents.reviewer.persist import save_review_artifacts
from meshpipeline.application.viewer_payload import _last_review


def _save(ws, verdict_result):
    save_review_artifacts(ws / "review_1", [{"role": "user", "content": "look"}], verdict_result,
                          {}, tool_call_count=0, retry_count=0, job_id="4f18812f")
    return json.loads((ws / "review_1" / "verdict.json").read_text())


def test_a_review_that_stopped_without_a_verdict_records_none(tmp_path):
    # job 4f18812f: the reviewer's model provider refused the first call, the mesh was delivered
    # with the review stated as unfinished - and this file said "FAIL", which the status served
    saved = _save(tmp_path, None)
    assert saved["verdict"] is None
    assert _last_review(tmp_path)["verdict"] is None


def test_a_concluded_review_keeps_its_verdict(tmp_path):
    for verdict in ("PASS", "FAIL"):
        saved = _save(tmp_path, {"verdict": verdict, "reasoning": "r", "axis_findings": []})
        assert saved["verdict"] == verdict
        assert _last_review(tmp_path)["verdict"] == verdict


def test_a_result_that_names_no_real_verdict_records_none(tmp_path):
    assert _save(tmp_path, {"reasoning": "no verdict key"})["verdict"] is None
    assert _save(tmp_path, {"verdict": ""})["verdict"] is None
