# Responsibility: Pin the CRS v5 beta result contract (`RepairResultV5`), its plain wording and the progress mapping
#                 (docs/superpowers/specs/2026-11-26-crs-v5-beta-ux.md "Contract" and "User-facing language").
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.seq import SeqCounters
from meshpipeline.cad.repair_v5 import events as EV
from meshpipeline.cad.repair_v5 import result as R

CONTRACT_FIELDS = {"engine", "choice", "outcome", "problems", "meshers_passing", "meshers_total", "max_change_mm",
                   "allowed_change_mm", "volume_ratio", "size_ratio", "flags", "seconds", "spend_usd",
                   "needs_review", "auto_accepted"}
PROBLEM_FIELDS = {"id", "type", "location", "status", "reason_text", "change_mm"}


def _d(i: str, cls: str, code: str = "overlapping_facets") -> Defect:
    return Defect(id=i, kind="mesher", code=code, entities=("F3", "B1"), xyz=((1.0, 2.0, 3.0),), cls=cls)


def _verdict(clean: bool, passes: int = 4, dev: float = 0.01, bound: float = 0.5, vol: float = 1.0,
             bbox: float = 1.0) -> SimpleNamespace:
    panel = [SimpleNamespace(status="pass" if k < passes else "fail") for k in range(4)]
    faithful = SimpleNamespace(max_dev_mm=dev, bound_mm=bound, volume_ratio=vol, bbox_ratio=bbox)
    return SimpleNamespace(clean=clean, panel=panel, faithful=faithful)


def test_the_spec_table_is_complete_and_plain():
    assert set(R.PROBLEM_TYPES) == {"close_faces", "overlap_self_intersection", "surface_crossing",
                                    "facet_crossing_local", "low_quality", "open_shell_volume",
                                    "invalid_brep_tolerance", "closed_periodic_face", "unknown"}
    assert R.PROBLEM_TYPES["close_faces"][1] == "thin gap"
    for _key, words in R.PROBLEM_TYPES.values():
        assert R.is_plain(words), words


def test_every_reason_text_is_plain():
    texts = [R.FIXED_TEXT, R.NOT_FIXED_TEXT, R.NOT_REACHED_TEXT, R.NOT_KEPT_TEXT, *R._LEFT_AS_IS.values()]
    for t in texts:
        assert R.is_plain(t), t


@pytest.mark.parametrize("bad", ["tournament lost", "entrant 3", "rule seed won", "fallback ran", "conformal pass",
                                 "close_faces", "split_aligned", "RuntimeError: boom", "gmsh failed"])
def test_internal_words_are_never_plain(bad):
    assert not R.is_plain(bad)


def test_a_partly_repaired_run_maps_statuses_and_carries_every_field():
    c = SeqCounters()
    a, b, x = _d("s1", "close_faces"), _d("s2", "low_quality"), _d("s3", "surface_crossing")
    for d in (a, b, x):
        c.tally("found", d)
    c.tally("fixed", a)
    c.tally("skipped", b)
    c.skipped_why["wall"] += 1
    c.tally("skipped", x)
    c.skipped_why["no_survivor"] += 1
    won = SimpleNamespace(defect_id="s1", result="won", deviation_mm=0.02)
    out = R.from_v5(choice="meshing", input_verdict=_verdict(False, 1), final_verdict=_verdict(False, 3),
                    delivered=True, history=c.history, matches=[won], seconds=12.34, spend_usd=0.5)
    d = out.to_dict()
    assert set(d) == CONTRACT_FIELDS
    assert all(set(p) == PROBLEM_FIELDS for p in d["problems"])
    assert d["engine"] == "v5" and d["outcome"] == "partly_repaired"
    assert [(p["type"], p["status"]) for p in d["problems"]] == [
        ("thin_gap", "fixed"), ("tiny_feature", "left_as_is"), ("intersecting_surfaces", "not_fixed")]
    assert d["problems"][0]["change_mm"] == 0.02
    assert d["problems"][0]["location"] == {"faces": ["F3"], "bodies": ["B1"], "points": [[1.0, 2.0, 3.0]]}
    assert (d["meshers_passing"], d["meshers_total"]) == (3, 4)
    assert d["max_change_mm"] == 0.01 and d["allowed_change_mm"] == 0.5
    assert d["auto_accepted"] and not d["needs_review"]
    for p in d["problems"]:
        assert R.is_plain(p["reason_text"]), p["reason_text"]


def test_a_clean_delivery_fixes_everything_and_no_delivery_fixes_nothing():
    c = SeqCounters()
    a = _d("s1", "close_faces")
    c.tally("found", a)
    c.tally("fixed", a)
    b = _d("s2", "open_shell_volume")
    c.tally("found", b)
    clean = R.from_v5(choice="meshing", input_verdict=_verdict(False), final_verdict=_verdict(True),
                      delivered=True, history=c.history, matches=(), seconds=1, spend_usd=0)
    assert clean.outcome == "repaired" and {p.status for p in clean.problems} == {"fixed"}
    kept_nothing = R.from_v5(choice="meshing", input_verdict=_verdict(False), final_verdict=_verdict(False),
                             delivered=False, history=c.history, matches=(), seconds=1, spend_usd=0)
    assert kept_nothing.outcome == "unrepairable"
    assert kept_nothing.problems[0].status == "not_fixed"
    assert kept_nothing.problems[0].reason_text == R.NOT_KEPT_TEXT
    assert not kept_nothing.needs_review and not kept_nothing.auto_accepted


def test_a_second_turn_does_not_leave_a_stale_skip():
    from meshpipeline.cad.repair_v4.seq import _untally_skip

    c = SeqCounters()
    a = _d("s1", "close_faces")
    c.tally("found", a)
    c.tally("skipped", a)
    c.skipped_why["slice"] += 1
    _untally_skip(c, a, "slice")
    c.tally("fixed", a)
    assert c.skipped_why == {}
    got = R.problems_from(c.history, delivered=True, clean=False)
    assert got[0].status == "fixed"


def test_flags_send_a_delivery_to_review():
    out = R.from_v5(choice="standalone", input_verdict=_verdict(False), final_verdict=_verdict(True, vol=1.4),
                    delivered=True, history=(), matches=(), seconds=1, spend_usd=0)
    assert out.flags == ("volume_changed",) and out.needs_review and not out.auto_accepted
    beyond = R.from_v5(choice="meshing", input_verdict=_verdict(False), final_verdict=_verdict(True, dev=0.3),
                       delivered=True, history=(), matches=(), seconds=1, spend_usd=0, auto_accept_mm=0.1)
    assert beyond.needs_review


def test_v3_contract():
    out = R.from_v3(choice="meshing", delivered=True, seconds=3)
    assert out.engine == "v3" and out.outcome == "repaired" and out.problems == ()
    assert set(out.to_dict()) == CONTRACT_FIELDS


def test_progress_mapper_speaks_plainly_and_in_order():
    m = R.ProgressMapper()
    recs = [{"event": "v5_start"}, {"event": "phase", "phase": "read"}, {"event": "diagnosed", "defects": 12},
            {"event": "phase", "phase": "conformal"},
            {"event": "phase", "phase": "tournament", "problem_index": 3, "problem_total": 12,
             "defect_class": "close_faces"},
            {"event": "defect_result", "what": "fixed", "defect_class": "close_faces"},
            {"event": "phase", "phase": "tournament", "problem_index": 4, "problem_total": 12,
             "defect_class": "unknown"},
            {"event": "defect_result", "what": "skipped", "defect_class": "unknown"},
            {"event": "phase", "phase": "fallback"}, {"event": "phase", "phase": "round_panel"},
            {"event": "phase", "phase": "deliver"}, {"event": "v5_done", "fixed": 10, "total": 12}]
    got = [e for e in (m(r) for r in recs) if e is not None]
    texts = [e["text"] for e in got]
    assert texts == ["Checking your CAD", "Found 12 problems to fix", "Lining up parts that should touch",
                     "Repairing a thin gap - problem 3 of 12", "Fixed", "Repairing a problem - problem 4 of 12",
                     "Couldn't fix this one - left as is", "Trying a broader repair", "Checking the repaired part",
                     "Repair finished: 10 of 12 fixed"]
    assert [e["stage"] for e in got][-3:] == ["broader_repair", "verifying", "done"]
    assert got[3]["problem_type"] == "thin_gap" and got[3]["problem_index"] == 3
    assert all(set(e) == {"stage", "problem_index", "problem_total", "problem_type", "text"} for e in got)
    assert all(R.is_plain(t) for t in texts)


def test_listeners_get_events_with_the_log_off(monkeypatch):
    monkeypatch.delenv("CRS_V5_EVENT_LOG", raising=False)
    monkeypatch.delenv("CRS_V5_EVENT_STDOUT", raising=False)
    got: list = []
    EV.add_listener(got.append)
    try:
        EV.enter("tournament", problem_index=1, problem_total=2, defect_class="close_faces")
        EV.emit("diagnosed", defects=2)
    finally:
        EV.remove_listener(got.append)
    EV.emit("after", x=1)
    assert [r["event"] for r in got] == ["phase", "diagnosed"]
    assert got[0]["phase"] == "tournament" and got[0]["problem_index"] == 1
    EV.set_context(**dict.fromkeys(("phase", *EV.NO_DEFECT)))


def test_counters_still_pickle_as_plain_counts():
    import copy

    c = SeqCounters()
    c.skipped_why["wall"] += 2
    assert copy.deepcopy(c.skipped_why) == {"wall": 2}
    assert dataclasses.asdict(c)["skipped_why"] == {"wall": 2}
