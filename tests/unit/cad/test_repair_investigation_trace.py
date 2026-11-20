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


# ---------------------------------------------------------------- the product collector

def test_collector_receives_what_the_investigator_hands_its_module_sink(monkeypatch):
    from meshpipeline.cad.repair import investigator as inv
    monkeypatch.setattr(inv, "sink", None)
    collector = it.start()
    try:
        inv.sink({"region": 0, "end": "runaway"})  # what `close` does with globals()["sink"]
    finally:
        it.stop(collector)
    assert collector.records == [{"region": 0, "end": "runaway"}]


def test_collector_outside_a_run_collects_nothing(monkeypatch):
    from meshpipeline.cad.repair import investigator as inv
    monkeypatch.setattr(inv, "sink", None)
    collector = it.start()
    it.stop(collector)
    assert inv.sink is None  # the product's module sink is put back once no run collects
    it.dispatch({"region": 9})  # a stray record with no collector open goes nowhere
    assert collector.records == []


def test_collector_chains_a_harness_sink_rather_than_replacing_it(monkeypatch):
    from meshpipeline.cad.repair import investigator as inv
    seen = []
    monkeypatch.setattr(inv, "sink", seen.append)
    collector = it.start()
    try:
        inv.sink({"region": 1})
    finally:
        it.stop(collector)
    assert seen == [{"region": 1}]
    assert collector.records == [{"region": 1}]


def test_summary_keeps_the_latest_record_per_region_and_caps_the_lines():
    first = _record(end="budget_spent")
    later = _record(end="runaway")
    other = {**_record(end="skipped"), "region": 2, "messages": []}
    out = it.summary([first, later, other], job_id="j", bytes_max=100_000)
    assert [o["region"] for o in out["outcomes"]] == [0, 2]
    assert out["outcomes"][0]["end"] == "runaway"
    assert {ln["region"] for ln in out["lines"]} == {0, 2}
    small = it.summary([later] * 1, job_id="j", bytes_max=1_500)
    assert sum(len(json.dumps(ln)) + 1 for ln in small["lines"]) <= 1_500
    assert small["truncated"] is True


# ---------------------------------------------------------------- end to end, real investigator

from tests.unit.cad.test_repair_investigator_live_scratch import (  # noqa: E402
    DEFECT,
    FP,
    ORIGINAL,
    REGION,
    _Call,
    _Reply,
    _Router,
    _Shape,
    live,  # noqa: F401 - the fixture
)


def test_a_real_live_investigation_reaches_the_collector_with_its_edit_verdicts(live,  # noqa: F811
                                                                                  monkeypatch):
    from meshpipeline.cad.repair import investigator as inv
    from meshpipeline.cad.repair import model_author as ma

    monkeypatch.setattr(inv, "sink", None)
    monkeypatch.setattr(ma, "converse_tools_sync", _Router(
        _Reply(tool_calls=[_Call("edit", {"op": "fix_small_edges", "fingerprint": FP})]),
        _Reply(text=json.dumps({"tool": "conclude", "args": {"package": {
            "intent": "remove the small edge", "cleared": [FP], "unchanged": [],
            "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}}}))))
    collector = it.start()
    try:
        author, _ = ma.select_author(_Shape("part"), original=ORIGINAL, job_id="j",
                                     tolerance_mm=0.01)
        author.observe_part((DEFECT,))
        author(REGION, (DEFECT,), ())
    finally:
        it.stop(collector)
    out = it.summary(collector.records, job_id="j")
    [row] = out["outcomes"]
    assert row["end"] == "concluded" and row["form"] == "package"
    assert row["edits"] == {"tried": 1, "kept": 1, "rolled_back": {}}
    edits = [ln["edit"] for ln in out["lines"] if ln.get("edit")]
    assert edits and edits[0]["kept"] is True and edits[0]["op"] == "fix_small_edges"


def test_begin_if_enabled_is_off_by_default(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATION_TRACE", False, raising=False)
    assert it.begin_if_enabled() is None
    assert it.finish(None) is None


def test_begin_if_enabled_collects_and_finish_summarises(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair import investigator as inv
    monkeypatch.setattr(inv, "sink", None)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATION_TRACE", True, raising=False)
    collector = it.begin_if_enabled()
    inv.sink(_record())
    out = it.finish(collector)
    assert inv.sink is None
    assert [o["end"] for o in out["outcomes"]] == ["runaway"]
    assert out["lines"]


def test_finish_with_no_investigation_says_so(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair import investigator as inv
    monkeypatch.setattr(inv, "sink", None)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATION_TRACE", True, raising=False)
    assert it.finish(it.begin_if_enabled()) == {"outcomes": [], "lines": [], "truncated": False}


# ---------------------------------------------------------------- elided conversations (Greptile)

def _elide(message, call):
    """What `_Investigation.elide` does to an old tool result past CONVERSATION_BYTES_MAX."""
    return {**message, "content": json.dumps({"elided": True, "call": call, "tool": "edit",
                                              "sha256": "x"}, sort_keys=True)}


def _edit_turn(n, ok=True, args='{"op": "fragment_bodies"}'):
    return {"n": n, "tool": "edit", "args": args, "ok": ok, "error": "", "result_bytes": 9,
            "result_sha256": "x", "seconds": 1.0}


def test_outcome_counts_edits_whose_results_were_elided():
    rec = _record()
    rec["messages"][5] = _elide(rec["messages"][5], 2)   # the target_remains edit
    rec["messages"][7] = _elide(rec["messages"][7], 3)   # the declined edit
    rec["turns"] = [rec["turns"][0], _edit_turn(2), _edit_turn(3)]
    row = it.outcome(rec)
    assert row["edits"]["tried"] == 2
    assert row["edits"]["kept"] == 0
    assert row["edits"]["rolled_back"] == {"unknown (result elided)": 2}
    assert "no edit tried" not in row["why_no_package"]


def test_outcome_mixes_visible_and_elided_rollbacks_and_reads_kept_from_the_log():
    rec = _record(edits=[{"op": "sew_faces", "kept": True}])
    rec["messages"][5] = _elide(rec["messages"][5], 2)
    rec["turns"] = [rec["turns"][0], _edit_turn(2), _edit_turn(3), _edit_turn(4)]
    row = it.outcome(rec)
    assert row["edits"]["tried"] == 3
    assert row["edits"]["kept"] == 1
    assert row["edits"]["rolled_back"] == {"declined": 1, "unknown (result elided)": 1}


def test_outcome_does_not_count_refused_edits_or_resets_as_tried():
    rec = _record(messages=[])
    rec["turns"] = [_edit_turn(1, ok=False), _edit_turn(2, args='{"reset": true}')]
    assert it.outcome(rec)["edits"]["tried"] == 0


def test_trace_marks_an_elided_result_rather_than_dropping_the_call():
    rec = _record()
    rec["messages"][5] = _elide(rec["messages"][5], 2)
    calls = [ln for ln in it.trace(rec) if ln["kind"] == "call"]
    assert [c["tool"] for c in calls] == ["look", "edit", "edit"]
    assert calls[1]["elided"] is True and "edit" not in calls[1]


# ---------------------------------------------------------------- the review, for audit only

_REJECTED = {"region_index": 0, "status": "reverted", "reason": "review: rejected by review",
             "reviewed": "reject", "review_tier": "reviewer",
             "review_reason": "REVIEWER PROSE the customer must never see",
             "review_objection": {"field": "deviation_mm", "measured": 0.42},
             "review_evidence": {"big": "blob"}}


def test_review_of_keeps_the_measured_objection_and_drops_the_prose():
    got = it.review_of(_REJECTED)
    assert got == {"region": 0, "status": "reverted", "reason": "review: rejected by review",
                   "reviewed": "reject", "review_tier": "reviewer",
                   "objection": {"field": "deviation_mm", "measured": 0.42}}
    assert "PROSE" not in json.dumps(got)


def test_review_of_an_unreviewed_package_says_so_and_invents_nothing():
    got = it.review_of({"region_index": 1, "status": "committed", "reason": ""})
    assert got == {"region": 1, "status": "committed", "reason": "", "reviewed": "",
                   "review_tier": "", "objection": None}


def test_with_reviews_attaches_each_regions_packages_to_its_outcome_row():
    traced = {"outcomes": [{"region": 0, "end": "concluded"}, {"region": 2, "end": "runaway"}],
              "lines": [], "truncated": False}
    out = it.with_reviews(traced, [_REJECTED, {"region_index": 0, "status": "committed",
                                               "reason": ""}])
    assert [p["status"] for p in out["outcomes"][0]["packages"]] == ["reverted", "committed"]
    assert out["outcomes"][0]["packages"][0]["objection"]["field"] == "deviation_mm"
    assert out["outcomes"][1]["packages"] == []
    assert "PROSE" not in json.dumps(out)


# --- the MEASURED evidence a verdict was made on, in the row -----------------------------------
# Two gate runs at one commit judged the SAME operation on the SAME target - fix_small_faces on
# face:c678dc263c3ceafd - and reached opposite verdicts, one reject naming
# mesh_after=["mesh_edge_through_face"] and one accept. Nobody could say whether the reviewer saw
# different evidence or behaved differently, because `review_of` kept the verdict and dropped what
# it was made on. The measured half now travels with it; the prose still does not.

def _evidence() -> dict:
    """An evidence block shaped as `package_review.evidence_of` builds one, prose included so the
    exclusion is actually tested."""
    return {
        "ops": [{"op": "fix_small_faces", "status": "kept",
                 "reason": "PROSE: the sliver was debris"}],
        "area_mm2": {"declared": 0.0, "measured": 0.342214},
        "volume_mm3": {"declared": 0.0, "measured": 0.0},
        "faces": {"before": 89, "after": 86},
        "solids": {"before": 2, "after": 2},
        "diagonal_mm": 176.4,
        "area_mm2_before": 31293.4,
        "volume_mm3_before": 8000.0,
        "deviation_status": "measured",
        "deviation_basis": "whole_part",
        "deviation_mm": 0.0012,
        "deviation_samples": 240,
        "deviation_faces": 86,
        "deviation_faces_skipped": 0,
        "removed": {"faces": 3, "area_mm2": 0.342214},
        "mesh_before": ["edge_through_face"],
        "mesh_after": [],
        "defects_cleared": ["small_face"],
        "defects_revealed": [],
        "defects_remaining": ["small_edge"],
        "review_rejects": {"count": 1, "reasons": ["PROSE: an earlier reject's words"]},
        "author_claim": "PROSE: remove the two sliver faces",
    }


def _package(evidence: dict | None) -> dict:
    return {"region_index": 0, "status": "committed", "reason": "", "reviewed": "accept",
            "review_tier": "reviewer", "review_objection": None, "review_evidence": evidence}


def test_the_measured_evidence_travels_with_the_verdict():
    row = it.review_of(_package(_evidence()))
    measured = row["evidence"]

    assert measured["mesh_after"] == [] and measured["mesh_before"] == ["edge_through_face"]
    assert measured["deviation_mm"] == 0.0012
    assert measured["deviation_basis"] == "whole_part"
    assert measured["deviation_samples"] == 240
    assert measured["deviation_status"] == "measured"
    assert measured["removed"] == {"faces": 3, "area_mm2": 0.342214}
    assert measured["defects_cleared"] == ["small_face"]
    assert measured["defects_revealed"] == []
    assert measured["area_mm2"] == {"declared": 0.0, "measured": 0.342214}
    assert measured["faces"] == {"before": 89, "after": 86}
    # The operation list keeps WHAT ran and how it ended, never why.
    assert measured["ops"] == [{"op": "fix_small_faces", "status": "kept"}]
    # An earlier reject is a COUNT here, not its words.
    assert measured["review_rejects"] == {"count": 1}
    assert row["review_tier"] == "reviewer"


def test_no_prose_reaches_the_row():
    """THE RULE THIS PROJECTION MUST NOT BREAK: the row carries measurements, never the reviewer's
    or the author's words. Every prose value in the fixture is marked, so one assertion catches
    any of them arriving by any route - including a key added later."""
    import json

    row = it.review_of(_package(_evidence()))

    assert "PROSE" not in json.dumps(row, sort_keys=True, default=str)
    assert "author_claim" not in row["evidence"]
    assert "reasons" not in row["evidence"]["review_rejects"]


def test_a_package_with_no_evidence_gains_no_key():
    assert "evidence" not in it.review_of(_package(None))
    assert "evidence" not in it.review_of({"region_index": 0, "status": "reverted"})


def test_clipped_evidence_is_reported_as_clipped():
    """`clip_evidence` replaces an over-long block with a stated marker. The row says so rather
    than inventing an empty measurement."""
    row = it.review_of(_package({"clipped": "evidence clipped to 4096 of 9001 bytes"}))

    assert row["evidence"] == {"clipped": "evidence clipped to 4096 of 9001 bytes"}
