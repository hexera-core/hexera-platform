# Responsibility: Verify the investigation trace - the per-region outcome row ("why no package")
#                 and the redacted, size-capped per-turn trace - read from the record the
#                 investigator hands its sink.
# Boundaries: hermetic - synthetic records in the exact shape `_Investigation.to_dict()` plus
#             `messages` produces; no CAD, no model, no storage.
from __future__ import annotations

import json

from meshpipeline.cad.repair import investigation_trace as it


def _edit_result(call, *, status="rolled_back", reason="target_remains", kept=False,
                 op="fragment_bodies", fingerprint="face:00c7"):
    return json.dumps({"tool_result": {"call": call, "tool": "edit", "ok": True, "error": "",
                                       "data": {"op": op, "fingerprint": fingerprint,
                                                "status": status, "reason": reason,
                                                "kept": kept, "cleared": [],
                                                "remaining": [fingerprint], "new": [],
                                                "counts": {"faces": 12}}}})


def _record(*, end="runaway", form="", reason="the same call was asked 3 times",
            messages=None, edits=()):
    msgs = messages if messages is not None else [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "{\"investigate\": {\"region\": 0}}"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "look", "arguments": "{\"what\": \"region\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "look",
         "content": json.dumps({"tool_result": {"call": 1, "tool": "look", "ok": True,
                                                "error": "",
                                                "data": {"faces": ["SECRET GEOMETRY"]}}})},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "edit",
                          "arguments": "{\"op\": \"fragment_bodies\", \"fingerprint\": "
                                       "\"face:00c7\"}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "edit", "content": _edit_result(2)},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c3", "type": "function",
             "function": {"name": "edit", "arguments": "{\"op\": \"sew_faces\"}"}}]},
        {"role": "tool", "tool_call_id": "c3", "name": "edit",
         "content": _edit_result(3, reason="declined", op="sew_faces")},
    ]
    conclusion = ({"form": form, "outcome": "package", "reason": ""} if form == "package"
                  else {"form": form, "outcome": end, "reason": reason})
    return {"region": 0, "fingerprints": ["face:00c7"], "tier": "cheap",
            "budget": {"tool_calls": 8, "model_calls": 6, "wall_s": 150.0},
            "spent": {"tool_calls": 3, "model_calls": 3, "seconds": 41.2},
            "turns": [{"n": 1, "tool": "look", "args": "{}", "ok": True, "error": "",
                       "result_bytes": 10, "result_sha256": "x", "seconds": 1.0}],
            "edits": list(edits), "conclusion": conclusion, "end": end,
            "messages": msgs}


# ---------------------------------------------------------------- the outcome row

def test_outcome_counts_rolled_back_edits_by_reason():
    row = it.outcome(_record())
    assert row["region"] == 0
    assert row["end"] == "runaway"
    assert row["edits"] == {"tried": 2, "kept": 0,
                            "rolled_back": {"declined": 1, "target_remains": 1}}


def test_outcome_says_why_no_package_when_edits_were_rolled_back():
    why = it.outcome(_record())["why_no_package"]
    assert "runaway" in why
    assert "no edit kept" in why
    assert "target_remains" in why


def test_outcome_says_no_edit_tried_when_none_was():
    rec = _record(end="budget_spent", reason="the region's wall is spent",
                  messages=[{"role": "system", "content": "S"}])
    row = it.outcome(rec)
    assert row["edits"] == {"tried": 0, "kept": 0, "rolled_back": {}}
    assert "no edit tried" in row["why_no_package"]
    assert "the region's wall is spent" in row["why_no_package"]


def test_outcome_of_a_package_has_no_why():
    row = it.outcome(_record(end="concluded", form="package",
                             edits=[{"op": "sew_faces", "kept": True}]))
    assert row["form"] == "package"
    assert row["why_no_package"] == ""


def test_outcome_reads_a_record_without_messages():
    rec = _record()
    del rec["messages"]
    row = it.outcome(rec)
    assert row["edits"]["tried"] == 0
    assert row["end"] == "runaway"


# ---------------------------------------------------------------- the per-turn trace

def test_trace_has_one_entry_per_call_with_edit_status_and_reason():
    lines = it.trace(_record(), job_id="j1")
    calls = [ln for ln in lines if ln["kind"] == "call"]
    assert [c["tool"] for c in calls] == ["look", "edit", "edit"]
    edit = calls[1]
    assert edit["ok"] is True
    assert edit["edit"]["status"] == "rolled_back"
    assert edit["edit"]["reason"] == "target_remains"
    assert edit["edit"]["kept"] is False
    assert "fragment_bodies" in edit["args"]
    assert lines[-1]["kind"] == "end"
    assert lines[-1]["end"] == "runaway"
    assert all(ln["job_id"] == "j1" and ln["region"] == 0 for ln in lines)


def test_trace_keeps_no_customer_geometry_and_no_system_prompt():
    text = json.dumps(it.trace(_record(), job_id="j1"))
    assert "SECRET GEOMETRY" not in text
    assert "SYSTEM PROMPT" not in text


def test_trace_redacts_secrets_in_model_text():
    rec = _record(messages=[
        {"role": "assistant", "content": "use key sk-proj-ABCDEFGHIJKLMNOPQRSTUV and "
                                         "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"}])
    text = json.dumps(it.trace(rec, job_id="j"))
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUV" not in text
    assert "eyJhbGciOiJIUzI1NiJ9" not in text
    assert "[redacted]" in text


def test_trace_is_capped_and_says_so():
    many = [{"role": "assistant", "content": "x" * 500} for _ in range(400)]
    lines = it.trace(_record(messages=many), job_id="j", bytes_max=4_000)
    size = sum(len(json.dumps(ln)) + 1 for ln in lines)
    assert size <= 4_000
    assert any(ln["kind"] == "truncated" for ln in lines)
    assert lines[-1]["kind"] == "end"
