"""CRS v5 beta: the repair's plain progress lane and its job API section.

THE HARD RULE (spec "User-facing language"): no string a customer reads may contain
"tournament", "entrant", "heat", "rule seed", "fallback", "conformal", class identifiers, op names
or exception text. Every surface that produces customer text - the progress lane, the job API's
repair section, the HTML report and the event constructor - is driven here with adversarial engine
output and every string it emits is checked."""
from __future__ import annotations

import re

import pytest

import meshpipeline.events as E
from meshpipeline.contracts import repair_plain as P
from meshpipeline.pipeline import repair_progress as RP
from meshpipeline.pipeline import repair_result as RR
from meshpipeline.render.repair_report import repair_report_html

#: What a customer must never read. Word-level, case-insensitive, plus identifier shapes.
BANNED_WORDS = re.compile(
    r"tournament|entrant|\bheats?\b|rule seed|\bseed|fallback|fall back|conformal|traceback|"
    r"exception|error\b|\bv[345]\b|\bpanel\b|\bkernel\b", re.I)
#: ...and identifier shapes: snake_case, camelCase, CamelCase (case-sensitive).
IDENTIFIERS = re.compile(r"\w+_\w+|\b[a-z]+[A-Z]\w*|\b[A-Z][a-z0-9]+[A-Z]\w*")

#: The engine's own words, as a v5 event log and result might carry them.
POISON = ["close_wire_gaps", "RepairRefused: tournament heat 3 lost", "fallback via conformal",
          "Traceback (most recent call last): File \"x.py\", line 3", "rule seed 12",
          "entrant op=remove_small", "KeyError('F3')", "overlap_self_intersection",
          "BRepAlgoAPI_Fuse failed", "heat survived"]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            # keys and the type KEY are machine fields the console maps through the plain table;
            # ids are opaque handles, faces/bodies are locations the viewer pins - none is prose
            if k in ("type", "problem_type", "id", "faces", "bodies", "choice", "engine",
                     "outcome", "status", "stage"):
                continue
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _assert_plain(texts):
    texts = list(texts)
    assert texts, "nothing was said"
    for text in texts:
        assert not BANNED_WORDS.search(text), f"a customer would read internal words: {text!r}"
        assert not IDENTIFIERS.search(text), f"a customer would read an identifier: {text!r}"
        assert not P.leaks(text), text


# ---------------------------------------------------------------- the lane, as the spec says it

V5_RUN = [
    {"event": "file_start", "file_name": "part.step"},
    {"event": "phase", "phase": "diagnose", "round": 1},
    {"event": "checker_failed", "reason": "close_wire_gaps"},
    {"event": "phase", "phase": "tournament", "defect_id": "d1", "defect_class": "close_faces"},
    {"event": "heat", "result": "survived", "op": "close_wire_gaps"},
    {"event": "local_verify", "result": "gone", "defect_id": "d1", "defect_class": "close_faces"},
    {"event": "phase", "phase": "tournament", "defect_id": "d2",
     "defect_class": "overlap_self_intersection"},
    {"event": "kernel_call_failed", "error": "KeyError('F3')"},
    {"event": "phase", "phase": "fallback", "wall_left_s": 30},
    {"event": "phase", "phase": "round_panel"},
    {"event": "file_end", "result": "done"},
]


def test_the_lane_says_the_spec_lines_in_order():
    lines = RP.map_events(V5_RUN)
    assert [(ln["stage"], ln["text"]) for ln in lines] == [
        ("checking", "Checking your CAD"),
        ("repairing", "Repairing a thin gap — problem 1"),
        ("repairing", "Fixed"),
        ("repairing", "Repairing overlapping parts — problem 2"),
        ("repairing", "Couldn't fix this one — left as is"),
        ("broader_repair", "Trying a broader repair"),
        ("verifying", "Checking the repaired part"),
    ]
    assert lines[1]["problem_type"] == "close_faces" and lines[1]["problem_index"] == 1


def test_a_count_from_the_engine_numbers_every_problem():
    lines = RP.map_events([{"event": "diagnosed", "defects_found": 12},
                           {"event": "phase", "phase": "tournament", "defect_id": "a",
                            "defect_class": "close_faces"}])
    assert lines[0]["text"] == "Found 12 problems to fix"
    assert lines[1]["text"] == "Repairing a thin gap — problem 1 of 12"
    assert lines[1]["problem_total"] == 12


def test_the_engines_own_repair_progress_is_re_said_from_templates():
    lines = RP.map_events([{"event": "repair_progress", "stage": "repairing", "problem_index": 3,
                            "problem_total": 12, "problem_type": "close_faces",
                            "text": "tournament heat 3: entrant close_wire_gaps"}])
    assert lines == [RP.progress("repairing", "Repairing a thin gap — problem 3 of 12",
                                 index=3, total=12, kind="close_faces")]


def test_done_line():
    assert RP.done_line(10, 12) == "Repair finished: 10 of 12 fixed"


def test_the_lane_never_says_an_engine_string():
    poisoned = []
    for word in POISON:
        poisoned += [{"event": "phase", "phase": "tournament", "defect_id": word,
                      "defect_class": word},
                     {"event": "repair_progress", "stage": "repairing", "text": word,
                      "problem_type": word, "status": word},
                     {"event": "repair_progress", "stage": "done", "text": word},
                     {"event": "wall_stop", "reason": word},
                     {"event": "local_verify", "result": "gone", "defect_id": word}]
    lines = RP.map_events(V5_RUN + poisoned)
    _assert_plain(ln["text"] for ln in lines)
    for ln in lines:
        assert ln["problem_type"] in (None, *P.PROBLEM_TYPES)


def test_the_event_constructor_refuses_a_leaking_line():
    for word in POISON:
        with pytest.raises(ValueError):
            E.repair_progress("repair", {"stage": "repairing", "text": word})
    wire = E.repair_progress("repair", RP.progress("done", RP.done_line(10, 12), total=12)).wire()
    assert wire["type"] == "repair_progress" and wire["stage"] == "repair"
    assert wire["progress"]["stage"] == "done" and wire["progress"]["problem_total"] == 12
    assert wire["type"] in E.EVENT_TYPES


# ---------------------------------------------------------------- the section and the report

def _result(**over):
    base = {"engine": "v5", "outcome": "partly_repaired", "meshers_passing": 3, "meshers_total": 4,
            "max_change_mm": 0.4, "allowed_change_mm": 0.1, "volume_ratio": 1.0004,
            "size_ratio": 1.0, "flags": ["The part's volume changed slightly"] + POISON,
            "seconds": 81.0, "spend_usd": 0.7, "needs_review": True, "auto_accepted": False,
            "problems": [
                {"id": "p1", "type": "close_faces", "status": "fixed", "change_mm": 0.4,
                 "reason_text": "Closed the gap", "location": {"faces": ["F12"],
                                                              "points": [[1, 2, 3]]}},
                *[{"id": f"x{i}", "type": word, "status": "left_as_is", "reason_text": word,
                   "location": {"faces": [], "bodies": []}} for i, word in enumerate(POISON)]]}
    return {**base, **over}


def _state(result, **record):
    return {"repair_attempt": {"attempted": True, "result": {"engine_result": result,
                                                              "needs_review": True, **record}}}


def test_the_section_carries_the_contract_fields():
    sec = RR.section(_state(_result(), auto_accepted=True, review_bypass=True),
                     choice="meshing")
    for key in ("engine", "choice", "outcome", "problems", "meshers_passing", "meshers_total",
                "max_change_mm", "allowed_change_mm", "volume_ratio", "size_ratio", "flags",
                "seconds", "spend_usd", "needs_review", "auto_accepted"):
        assert key in sec, key
    assert sec["auto_accepted"] and sec["needs_review"] and sec["choice"] == "meshing"
    assert sec["problems"][0]["type_text"] == "thin gap"
    assert sec["counts"] == {"found": 1 + len(POISON), "fixed": 1, "not_fixed": 0,
                             "left_as_is": len(POISON)}
    assert sec["flags"] == ["The part's volume changed slightly"]


def test_the_section_and_report_never_say_an_engine_string():
    sec = RR.section(_state(_result()), choice="standalone")
    _assert_plain(_strings(sec))
    for p in sec["problems"]:
        assert p["type"] in P.PROBLEM_TYPES
    page = re.sub(r"<style>.*?</style>|<title>.*?</title>", " ",
                  repair_report_html(sec, job_id="j"), flags=re.S)
    page = re.sub(r"<[^>]+>|&[a-z#0-9]+;", " ", page)   # tags and attributes are not read
    _assert_plain([page])


def test_a_declined_v3_repair_claims_no_counts():
    # found live on the personal slug: a refusal that parked 30 defects for review read
    # "Repair finished: nothing needed fixing"
    sec = RR.section({"repair_attempt": {"attempted": False, "outcome": "refused"}},
                     choice="meshing")
    assert sec["counts"] == {} and sec["outcome"] == "unrepairable"
    assert RR.summary_text(sec) == "Repair finished"
    # ...the same for the engine's v3 fallback, whose contract lists no problems (found live)
    sec = RR.section({"repair_attempt": {"attempted": False, "repair_result_v5": {
        "engine": "v3", "outcome": "unrepairable", "problems": [], "seconds": 185.1}}},
        choice="standalone")
    assert sec["engine"] == "v3" and sec["counts"] == {}
    assert RR.summary_text(sec) == "Repair finished"
    # ...and the run's own bypass survives a repair that made no review decision (found live)
    sec = RR.section({"repair_attempt": {"attempted": False, "outcome": "refused"},
                      "repair_goal": {"goal": "meshing", "auto_accept_repairs": True}},
                     choice="meshing")
    assert sec["review_bypass"] is True and sec["auto_accepted"] is False


def test_mesh_only_has_no_repair():
    assert RR.section({}, choice="off") == {"choice": "off", "ran": False, "needs_review": False,
                                            "auto_accepted": False, "review_bypass": False}


# ---------------------------------------------------------------- the review decision

def test_a_change_past_the_allowance_needs_review_and_becomes_items():
    result = _result()
    assert RR.needs_review(result)
    assert RR.needs_review({"max_change_mm": 0.5, "allowed_change_mm": 0.1})
    # the measured change decides even when the engine said no (PR 3's job: 0.274 vs 0.05 mm)
    assert RR.needs_review({"needs_review": False, "max_change_mm": 0.274,
                            "allowed_change_mm": 0.05, "outcome": "partly_repaired"})
    # ...but a repair that kept nothing changed nothing
    assert not RR.needs_review({"needs_review": False, "max_change_mm": 9,
                                "allowed_change_mm": 1, "outcome": "unrepairable"})
    assert not RR.needs_review({"max_change_mm": 0.01, "allowed_change_mm": 0.05})
    items = RR.review_items(result)
    assert [i["fingerprint"] for i in items] == ["p1"]
    assert items[0]["proposal"]["op"] == RR.ACCEPT_OP and items[0]["entity_index"] == 12
    _assert_plain([items[0]["message"]])


def _two_waiting():
    return _result(problems=[
        {"id": "a", "type": "close_faces", "status": "fixed", "change_mm": 0.4},
        {"id": "b", "type": "close_faces", "status": "fixed", "change_mm": 0.3}])


def test_one_acceptance_never_applies_the_other_change():
    # review finding on #542: accepting one item and leaving another must not apply both
    result = _two_waiting()
    one = [{"op": RR.ACCEPT_OP, "targets": ["a"]}]
    both = one + [{"op": RR.ACCEPT_OP, "targets": ["b"]}]
    assert not RR.accepted_by_person(one, result)
    assert RR.accepted_by_person(both, result)
    assert not RR.accepted_by_person([], result)


def test_a_review_no_problem_explains_still_has_an_item():
    # review finding on #542: needs_review from a whole-part change with no per-problem movement
    result = _result(problems=[{"id": "a", "type": "close_faces", "status": "fixed",
                                "change_mm": None}], needs_review=True, max_change_mm=0.6)
    items = RR.review_items(result)
    assert [i["fingerprint"] for i in items] == [RR.WHOLE_PART_ID]
    _assert_plain([items[0]["message"]])
    assert RR.accepted_by_person([{"op": RR.ACCEPT_OP, "targets": [RR.WHOLE_PART_ID]}], result)


def test_the_engines_flag_keys_are_said_in_words():
    # review finding on #542: `volume_changed` / `size_shrank` were dropped by the leak guard
    sec = RR.section(_state(_result(flags=["volume_changed", "size_shrank", "close_wire_gaps"])),
                     choice="meshing")
    assert sec["flags"] == ["The part's volume changed noticeably", "The part got smaller"]


def test_a_refused_change_is_never_reported_approved():
    # review finding on #542: needs_review with nothing applied read "Approved"
    from meshpipeline.render.repair_report import key_rows
    refused = dict(RR.section(_state(_result(), applied=False), choice="meshing"))
    assert refused["applied"] is False
    assert ("Review", "Approved by you") not in key_rows(refused)
    assert any(k == "Review" and v.startswith("Not applied") for k, v in key_rows(refused))
    approved = dict(RR.section(_state(_result(), accepted_by_person=True), choice="meshing"))
    assert ("Review", "Approved by you") in key_rows(approved)


def test_acceptances_are_read_never_played():
    directives = [{"op": RR.ACCEPT_OP, "targets": ["p1"]}, {"op": "sew_faces", "targets": []}]
    assert RR.accepted_by_person(directives)
    assert RR.without_acceptances(directives) == ({"op": "sew_faces", "targets": []},)


@pytest.mark.parametrize("bypass, accepted, review_on, applied, waiting, auto", [
    (False, False, True, False, True, False),   # default: waits for a person
    (True, False, True, True, False, True),     # the run's bypass: applied, auto-accepted
    (False, True, True, True, False, False),    # a person accepted it on the re-run
    (False, False, False, False, False, False),  # no review screen here: not applied at all
])
def test_the_review_decision(monkeypatch, bypass, accepted, review_on, applied, waiting, auto):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.pipeline.repair_attempt import _review_decision
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", review_on)
    state = {"repair_goal": {"goal": "meshing", **({"auto_accept_repairs": True} if bypass
                                                   else {})},
             "repair_directives": [{"op": RR.ACCEPT_OP, "targets": ["p1"]}] if accepted else []}
    got = _review_decision(state, _result(), ())
    assert (got["applied"], got["waiting_for_review"], got["auto_accepted"]) == \
        (applied, waiting, auto)
    assert got["needs_review"] and got["review_bypass"] is bypass


def test_nothing_past_the_allowance_is_simply_applied(monkeypatch):
    from meshpipeline.pipeline.repair_attempt import _review_decision
    # within the allowance: largest change 0.04 mm, allowed 0.1 mm, no problem past it
    got = _review_decision({}, _result(needs_review=False, max_change_mm=0.04, problems=[
        {"id": "p1", "type": "close_faces", "status": "fixed", "change_mm": 0.04}]), ())
    assert got["applied"] and not got["needs_review"] and not got["auto_accepted"]


def test_the_engines_result_is_found_wherever_pr1_puts_it():
    class _Report:
        repair_result_v5 = {"engine": "v5"}
    assert RR.extract({}, _Report()) == {"engine": "v5"}
    assert RR.extract({"repair_result_v5": {"engine": "v3"}}) == {"engine": "v3"}
    assert RR.extract({"something": 1}) is None


# ---------------------------------------------------------------- the relay from the engine's thread

#: What cad/repair_v5/result.ProgressMapper hands `on_progress`: already plain, engine type keys.
ENGINE_EVENTS = [
    {"stage": "checking", "problem_index": None, "problem_total": 2, "problem_type": None,
     "text": "Found 2 problems to fix"},
    {"stage": "repairing", "problem_index": 1, "problem_total": 2, "problem_type": "thin_gap",
     "text": "Repairing a thin gap - problem 1 of 2"},
    {"stage": "repairing", "problem_index": 1, "problem_total": 2, "problem_type": "thin_gap",
     "text": "Fixed"},
    {"stage": "repairing", "problem_index": 2, "problem_total": 2, "problem_type": "seam_problem",
     "text": "Left as is: there wasn't enough time to repair this one"},
    {"stage": "verifying", "problem_index": None, "problem_total": 2, "problem_type": None,
     "text": "Checking the repaired part"},
]


def test_the_relay_publishes_typed_events_from_the_engines_thread(monkeypatch):
    import asyncio
    import threading

    said: list[dict] = []
    rows: list[str] = []

    async def _publish(job_id, line, *, seq):
        said.append(dict(line))

    async def _astep(pub, kind, text, **kw):
        rows.append(text)

    monkeypatch.setattr(RP, "apublish", _publish)
    monkeypatch.setattr("meshpipeline.contracts.narration.astep", _astep)
    monkeypatch.setattr("meshpipeline.application.execution_publisher.execution_publisher",
                        lambda job_id, agent=None: object())

    async def _run():
        relay = RP.ProgressRelay("job-1", lane=RP.Lane("job-1")).start()
        worker = threading.Thread(target=lambda: [relay.push(e) for e in ENGINE_EVENTS])
        worker.start()
        await asyncio.to_thread(worker.join)
        await relay.stop()
        return relay

    relay = asyncio.run(_run())
    assert len(relay.events) == len(ENGINE_EVENTS) and len(rows) == len(ENGINE_EVENTS)
    assert [(s["stage"], s["text"], s.get("result"), s["problem_type"]) for s in said] == [
        ("checking", "Found 2 problems to fix", None, None),
        ("repairing", "Repairing a thin gap — problem 1 of 2", None, "close_faces"),
        ("repairing", "Fixed", "fixed", "close_faces"),
        ("repairing", "Couldn't fix this one — left as is", "left_as_is", "closed_periodic_face"),
        ("verifying", "Checking the repaired part", None, None)]
    _assert_plain(s["text"] for s in said)
    # the engine's "Fixed" carries no number; the lane gives it the problem in hand (found live)
    assert said[2]["problem_index"] == 1


def test_the_wire_is_flat_for_the_console_and_nested_for_the_contract():
    wire = E.repair_progress("repair", RP.progress("done", RP.done_line(10, 12), total=12,
                                                   fixed=10)).wire()
    assert wire["stage"] == "repair" and wire["step"] == "done"
    assert wire["problems_fixed"] == 10 and wire["problem_total"] == 12
    assert wire["progress"]["stage"] == "done" and wire["progress"]["problems_fixed"] == 10
    fixed = RP.map_events(V5_RUN)[2]
    assert fixed["result"] == "fixed" and fixed["problem_type"] == "close_faces"
    left = RP.map_events(V5_RUN)[4]
    assert left["result"] == "left_as_is" and left["problem_type"] == "overlap_self_intersection"


def test_the_viewer_skin_has_the_check_skins_shape_and_per_point_deviation():
    import base64

    import numpy as np

    from meshpipeline.application.repair_viewer import point_faces, surface_block
    xyz = np.array([[0, 0, 0], [1000, 0, 0], [0, 1000, 0], [0, 0, 1000]], dtype=float)
    tris = np.array([[0, 1, 2], [0, 1, 3]])
    faces = np.array([1, 2], dtype=np.uint32)
    per_point = np.array([0.0 if f == 1 else 0.4 for f in point_faces(4, tris, faces)])
    skin = surface_block(xyz, tris, faces, name="repaired", per_point=per_point)
    patch = skin["patches"][0]
    pts = np.frombuffer(base64.b64decode(patch["points_b64"]), dtype=np.float32)
    assert skin["mesh_units"] == "m" and pts.max() == 1.0            # millimetres -> metres
    polys = np.frombuffer(base64.b64decode(patch["polys_b64"]), dtype=np.uint32)
    assert polys.tolist() == [3, 0, 1, 2, 3, 0, 1, 3]
    dev = np.frombuffer(base64.b64decode(patch["deviation_b64"]), dtype=np.float32)
    assert len(dev) == len(pts) // 3


def test_the_engines_short_type_keys_read_back_to_their_class():
    # cad/repair_v5/result.PROBLEM_TYPES spells "close_faces" as "thin_gap"; the API speaks classes
    assert P.type_key("thin_gap") == "close_faces"
    assert P.type_key("overlapping_parts") == "overlap_self_intersection"
    assert P.type_key("unidentified") == "unknown" and P.type_key("nonsense") == "unknown"
    line = RP.map_events([{"event": "repair_progress", "stage": "repairing", "problem_index": 1,
                           "problem_total": 2, "problem_type": "thin_gap"}])[0]
    assert line["problem_type"] == "close_faces"
    assert line["text"].startswith("Repairing a thin gap")


def test_the_result_is_read_where_pr1_writes_it():
    class _Report:
        diagnostics = {"repair_result_v5": {"engine": "v5", "outcome": "repaired"}}
    assert RR.extract({}, _Report()) == {"engine": "v5", "outcome": "repaired"}
    # a refused repair carries it on the attempt record only (no review decision was made)
    sec = RR.section({"repair_attempt": {"attempted": False, "repair_result_v5": _result(
        problems=[{"id": "a", "type": "thin_gap", "status": "fixed", "change_mm": 0.01}],
        needs_review=False)}}, choice="meshing")
    assert sec["ran"] and sec["engine"] == "v5" and sec["problems"][0]["type"] == "close_faces"


def test_a_line_the_node_and_the_engine_both_say_is_said_once(monkeypatch):
    # found on the slug: "Checking your CAD" from the node, then again from the engine
    import asyncio

    said: list[dict] = []

    async def _publish(job_id, line, *, seq):
        said.append(dict(line))

    monkeypatch.setattr(RP, "apublish", _publish)

    async def _run():
        lane = RP.Lane("job-1")
        await lane.say(RP.progress(RP.CHECKING, RP.CHECKING_TEXT))
        await lane.feed({"event": "repair_progress", "stage": "checking",
                         "text": "Checking your CAD"})
        await lane.feed({"event": "repair_progress", "stage": "verifying"})
    asyncio.run(_run())
    assert [s["text"] for s in said] == ["Checking your CAD", "Checking the repaired part"]
