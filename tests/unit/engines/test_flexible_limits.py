# Responsibility: Verify NOTE-14 - how strict a limit is, in the user's words: the reading's strictness entries are checked (the quote is the user's, a number names only its own limit), applied in the order written to one limit, a group or all of them, refined by a later sentence and carried from the notes onto a saved standard's rows; a flexible limit is told with the share outside it, never "not met", a hard edge length is judged; with nothing said about strictness every report is the one it was.
# Boundaries: pure over plain data (the reader's answer as the model would give it); no model, no mesher.
from __future__ import annotations

import copy
import json

import pytest

from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import fea_requirements as FR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS

FOUNDER = "we're flexible around the minimum of 5mm, doesn't need to be a hard min"


def _answer(**fields) -> dict:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": [], "strictness": []}
    base.update(fields)
    return base


def _size(what, value, quote, unit="mm"):
    return {"what": what, "value": value, "unit": unit, "quote": quote}


def _lim(measure, direction, target, quote, shape="any"):
    return {"measure": measure, "shape": shape, "direction": direction, "target": target, "allowed_percent": 0,
            "outer": 0, "has_outer": False, "quote": quote}


def _strict(target, how, quote):
    return {"target": target, "strictness": how, "quote": quote}


def _read(notes: str, **fields):
    return MR.check(json.dumps(_answer(**fields)), notes)


def _rows(req) -> dict[str, dict]:
    return {r["field"]: r for r in req["rows"]}


def test_the_founders_sentence_makes_the_5_mm_minimum_flexible_and_nothing_else():
    notes = f"Min. element length 5 mm. Max. element length 15 mm. {FOUNDER}"
    req, dropped, na = _read(notes, sizes=[_size("min_edge", 5, "Min. element length 5 mm"),
                                           _size("max_edge", 15, "Max. element length 15 mm")],
                             strictness=[_strict("min_edge", "flexible", FOUNDER)])
    rows = _rows(req)
    assert dropped == [] and na == []                                    # the sentence is read, never "not applied"
    assert rows["min_edge"]["strictness"] == "flexible" and rows["min_edge"]["strictness_quote"] == FOUNDER
    assert "strictness" not in rows["max_edge"]
    assert MR.strictness(rows["min_edge"]) == "flexible" and MR.strictness(rows["max_edge"]) == "flexible"   # default
    assert "From your notes: shortest element edge over 5 mm - flexible - a target" in " ".join(
        MR.words(MR.section("used", requirements=req)))


def test_a_mixed_note_sets_each_limit_by_its_own_number():
    notes = "min 5 mm but flexible; max 15 mm is hard"
    req, dropped, _na = _read(notes, sizes=[_size("min_edge", 5, "min 5 mm but flexible"),
                                            _size("max_edge", 15, "max 15 mm is hard")],
                              strictness=[_strict("sizes", "flexible", "min 5 mm but flexible"),
                                          _strict("sizes", "hard", "max 15 mm is hard")])
    rows = _rows(req)
    # a group named with a number is only the limit of that number: the 5 mm flexible, the 15 mm hard
    assert rows["min_edge"]["strictness"] == "flexible" and rows["max_edge"]["strictness"] == "hard" and dropped == []


def test_groups_all_and_a_later_sentence_that_refines_an_earlier_one():
    notes = ("Min. element length 2 mm. Jacobian > 0.6. Warpage < 15. These are guidelines. "
             "But the warpage limit is hard.")
    req, _d, _na = _read(notes, sizes=[_size("min_edge", 2, "Min. element length 2 mm")],
                         limits=[_lim("jacobian", "min", 0.6, "Jacobian > 0.6"),
                                 _lim("warpage", "max", 15, "Warpage < 15")],
                         strictness=[_strict("all", "flexible", "These are guidelines."),
                                     _strict("warpage", "hard", "But the warpage limit is hard.")])
    rows = _rows(req)
    assert rows["min_edge"]["strictness"] == rows["limit:jacobian"]["strictness"] == "flexible"
    assert rows["limit:warpage"]["strictness"] == "hard"
    # a group: the minimums are every lower bound (the shortest edge, a Jacobian), never an upper one
    req, _d, _na = _read("Min. element length 2 mm. Jacobian > 0.6. Warpage < 15. The minimums are flexible.",
                         sizes=[_size("min_edge", 2, "Min. element length 2 mm")],
                         limits=[_lim("jacobian", "min", 0.6, "Jacobian > 0.6"), _lim("warpage", "max", 15,
                                                                                      "Warpage < 15")],
                         strictness=[_strict("minimums", "flexible", "The minimums are flexible.")])
    rows = _rows(req)
    assert rows["min_edge"]["strictness"] == rows["limit:jacobian"]["strictness"] == "flexible"
    assert "strictness" not in rows["limit:warpage"] and MR.strictness(rows["limit:warpage"]) == "hard"


def test_a_strictness_that_does_not_hold_changes_nothing_and_is_never_an_ask():
    """A strictness is no ask: words not the user's (the notes block's own "Use them as GUIDANCE" read as flexible) or
    naming no limit they set are set aside silently - never told "not applied", never counted (round 11: 8c and 2c
    each read "1 other thing your notes say about the mesh could not be applied")."""
    notes = "Min. element length 5 mm. We are flexible around the 8 mm minimum."
    req, dropped, na = _read(notes, sizes=[_size("min_edge", 5, "Min. element length 5 mm")],
                             strictness=[_strict("all", "flexible", "Use them as GUIDANCE for how to mesh"),
                                         _strict("min_edge", "flexible", "We are flexible around the 8 mm minimum.")])
    assert dropped == [] and na == [] and "strictness" not in _rows(req)["min_edge"]
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=na)
    assert MR.not_applied_words(sec) == [] and "Not applied" not in " ".join(MR.words(sec))


def test_soft_words_on_a_settings_own_line_make_that_row_flexible():
    """2c: "aim for y+ around 1 on the walls" - the y+ row is flexible; no orphan entry, nothing told not applied."""
    quote = "aim for y+ around 1 on the walls"
    req, dropped, na = MR.check(json.dumps(_answer(
        layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": quote}],
        strictness=[_strict("near_wall", "flexible", quote)])), quote, route="flow")
    row = next(r for r in req["rows"] if r["field"] == "layers:y_plus")
    assert row["strictness"] == "flexible" and dropped == [] and na == []
    assert MR.not_applied_words(MR.section("used", requirements=req)) == []


@pytest.mark.parametrize("quote,lo,hi", [("element size 1-2 mm", 1, 2), ("2 to 5 mm elements", 2, 5),
                                         ("elements between 1 and 3 mm", 1, 3)])
def test_a_size_range_for_the_whole_mesh_reads_as_its_two_ends(quote, lo, hi):
    """8c read "element size 1-2 mm" as one global size of 2 mm in round 11: a range is the shortest and the longest
    edge, both from the same quote (the prompt says so; the check keeps both ends as the user's own numbers)."""
    assert "A RANGE of sizes for the whole mesh" in MR.SYSTEM
    req, dropped, _na = _read(quote, sizes=[_size("min_edge", lo, quote), _size("max_edge", hi, quote)])
    assert dropped == [] and req["sizes"]["min_edge_mm"] == lo and req["sizes"]["max_edge_mm"] == hi
    assert req["sizes"]["global_mm"] is None


def _limit_row(met=False, outside=12, n=1000, **extra):
    return {"id": "r1", "field": "limit:warpage", "asked": "warpage under 15°", "applied": True, "met": met,
            "outside": outside, "outside_pct": MR.pct(outside, n), "delivered": "1.2% of elements (12) are past it, "
                                                                               "the worst 22°", **extra}


def test_a_flexible_limit_outside_in_places_is_told_never_not_met():
    rep = MR.finish_report([_limit_row(strictness="flexible")], 1000, "")
    row = rep["rows"][0]
    assert row["met"] is None and row["flexible"] is True and row["excess"] == 0 and rep["missed"] == 0
    assert MR.line(row) == "Warpage under 15°: flexible - 1.2% of elements (12) are past it, the worst 22°."
    assert MR.summary(rep) == "Of the 1 thing you asked for: 0 met; 1 flexible limit."
    assert MR.asks_said(rep) == "Warpage under 15°: flexible - 1.2% of elements (12) are past it, the worst 22°."
    # no limit of the verdict (neither met nor missed); the result's table tells it, "Flexible" with its share
    assert MR.asked_rows({"requirements": rep}) == []
    asked = MR.asked_rows({"requirements": rep}, told=True)
    assert [(a["label"], a["status"], a["shown"]) for a in asked] == [
        ("You asked: warpage under 15°", "Flexible", "1.2% of elements outside")]
    # said nothing about it: the limit as before - not fully met, and a row of the verdict
    rep = MR.finish_report([_limit_row()], 1000, "")
    assert rep["rows"][0]["met"] is False and rep["missed"] == 1 and "flexible" not in rep["rows"][0]


def test_a_hard_edge_length_is_judged_and_an_unstated_one_is_the_target_it_was():
    edge = {"id": "r1", "field": "min_edge", "asked": "a shortest element edge of 5 mm", "applied": True,
            "outside": 33, "outside_pct": 3.3, "target": True, "met": None, "worst": 3.1,
            "delivered": "3.3% of elements (33) have an edge shorter than 5 mm, the shortest 3.1 mm"}
    before = copy.deepcopy(edge)
    rep = MR.finish_report([copy.deepcopy(edge)], 1000, "")
    assert rep["rows"][0] == {**before, "excess": 0}                     # unstated: byte for byte as before
    rep = MR.finish_report([{**copy.deepcopy(edge), "strictness": "hard"}], 1000, "")
    assert rep["rows"][0]["met"] is False and rep["rows"][0]["delivered"].startswith("hard - 3.3% of elements")
    rep = MR.finish_report([{**copy.deepcopy(edge), "strictness": "flexible"}], 1000, "")
    assert rep["rows"][0]["met"] is None and rep["rows"][0]["delivered"].startswith("flexible - 3.3%")


def test_nothing_said_about_strictness_reads_and_reports_as_before():
    notes = "Min. element length 5 mm. Warpage < 15"
    fields = {"sizes": [_size("min_edge", 5, "Min. element length 5 mm")],
              "limits": [_lim("warpage", "max", 15, "Warpage < 15")]}
    req, dropped, na = _read(notes, **fields)
    assert "strictness" not in req and not any("strictness" in r for r in req["rows"])
    # an answer from before the field existed reads the same
    old = _answer(**fields)
    old.pop("strictness")
    assert MR.check(json.dumps(old), notes) == (req, dropped, na)
    assert "strictness" in MR.tool()["function"]["parameters"]["required"]


def test_the_notes_strictness_is_on_the_saved_standards_limit_too():
    std, _d, _na = _read("Min. Element Length > 5 mm", sizes=[_size("min_edge", 5, "Min. Element Length > 5 mm")])
    notes, _d, _na = _read(FOUNDER, strictness=[_strict("minimums", "flexible", FOUNDER)])
    merged = MS.merge(MR.section("used", requirements=std), MR.section("used", requirements=notes))
    row = next(r for r in merged["requirements"]["rows"] if r["field"] == "min_edge")
    assert row["from"] == "settings" and row["strictness"] == "flexible"
    assert "From your notes: shortest element edge over 5 mm - flexible - a target" in " ".join(merged["lines"])


def test_a_flexible_shortest_edge_lets_the_accuracy_zones_keep_their_size_on_a_solid():
    req, _d, _na = _read(f"Min. element length 5 mm. {FOUNDER}", sizes=[_size("min_edge", 5,
                                                                              "Min. element length 5 mm")],
                         strictness=[_strict("min_edge", "flexible", FOUNDER)])
    zones = [{"id": "P1", "kind": "part", "part": "Part_1", "size_m": 0.008},
             {"id": "R1", "kind": "round", "faces": ["Face_3"], "size_m": 0.002}]
    sizes = FR.whole_sizes({}, req, ["Part_1"], base_m=0.008)
    assert sizes["Part_1"]["min_flexible"] is True
    got = {z["id"]: z for z in P.user_sized(zones, sizes)}
    assert got["R1"]["size_m"] == 0.002                                   # under the flexible 5 mm: kept
    hard = {k: {kk: vv for kk, vv in v.items() if kk != "min_flexible"} for k, v in sizes.items()}
    assert {z["id"]: z for z in P.user_sized(zones, hard)}["R1"]["size_m"] == 0.005    # held, as before


def test_a_cfd_limit_neither_set_nor_measured_is_never_set_as_asked():
    """A saved criteria list's aspect ratio on VMTK or cfMesh (no setting there, not measured on its cells) read "Set
    as asked" and was counted "applied as a setting"; it is told not applied. Where the mesher holds it, as before."""
    from meshpipeline.engines import cfd_requirements as CR
    lim = {"measure": "aspect_ratio", "shape": "any", "op": "<=", "target": 4.0, "share": 0.01, "outer": 6.0}
    got = CR._limit_row(lim, {}, None, 1000, {})
    assert got["applied"] is False and got["delivered"].startswith("not applied - this mesher has no setting for")
    held = CR._limit_row(lim, {}, None, 1000, {"status": "applied", "built": "held at 4 by the mesher"})
    assert held["applied"] is True and held["delivered"].endswith("was not measured on this mesh (held at 4 by the mesher)")
