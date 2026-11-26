# Responsibility: Verify NOTE-14 on every surface that lists the user's asks: a limit the user SAID is flexible and that elements are outside of is told - "Flexible", with the share outside - in the result's table (solid FEA, shell, CFD report), is no limit of the verdict (neither met nor missed) and is counted a flexible limit in the tally; said flexible and inside, it is plainly met; said nothing, every surface is as before. And the plan never states a size the user did not ask as their ask.
# Boundaries: pure over plain data and a temporary workspace of records; no model, no mesher.
from __future__ import annotations

import copy
import json

from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import fea_requirements as FR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_delivery as SD
from meshpipeline.engines import verdict_limits as VL
from meshpipeline.render import mesh_report as RP

N = 1000


def _edge(outside: int, **extra) -> dict:
    """A min-edge row as the solid FEA route measures it (engines/fea_requirements.measure)."""
    return {"id": "r1", "field": "min_edge", "asked": "shortest element edge over 3 mm", "applied": True,
            "outside": outside, "outside_pct": MR.pct(outside, N), "target": True, "worst": 0.118,
            "met": True if outside == 0 else None,
            "delivered": "every element is inside it" if not outside else
            MR.edge_words("min_edge", outside, N, 3.0, 0.118), **extra}


def _quality(outside: int = 12, met: bool = False, **extra) -> dict:
    return {"id": "r1", "field": "limit:aspect_ratio", "asked": "aspect ratio under 3", "applied": True,
            "met": met, "outside": outside, "outside_pct": MR.pct(outside, N), "share": 0.0, "beyond": 0,
            "delivered": f"{MR.share_words(outside, N)} are over 3; the worst is 5.2" if outside else
            "every element is inside it", **extra}


def _q(row: dict) -> dict:
    return {"requirements": MR.finish_report([copy.deepcopy(row)], N, ""), "cells": N,
            "fea": {"plan": {"rules": {}}}}


def _table(q: dict, tmp_path) -> list[dict]:
    """The solid FEA result's check rows (engines/fea_results.results) for these records."""
    from meshpipeline.engines import fea_results as FRS
    (tmp_path / "fea_plan.json").write_text(json.dumps({"zones": []}))
    (tmp_path / "quality.json").write_text(json.dumps(q))
    return [r for r in FRS.results(tmp_path)["check"]["rows"] if str(r.get("key")).startswith("asked_")]


def test_f1_a_flexible_shortest_edge_almost_all_outside_is_told_flexible_never_met(tmp_path):
    q = _q(_edge(N, strictness="flexible"))
    rep = q["requirements"]
    # the verdict: information, neither a met limit nor a miss
    assert MR.asked_rows(q) == [] and not [r for r in VL.fea_limits(q) if r["key"].startswith("asked_")]
    assert rep["met"] == 0 and rep["missed"] == 0
    # the result's tables: told "Flexible" with the share outside - not "Met", not "Not met"
    told = MR.asked_rows(q, told=True)
    assert [(r["label"], r["told"], r["status"], r["shown"]) for r in told] == [
        ("You asked: shortest element edge over 3 mm", "target", "Flexible", "100% of elements under")]
    rows = _table(q, tmp_path)
    assert [(r["ok"], r["status"], r["shown"]) for r in rows] == [(None, "Flexible", "100% of elements under")]
    assert [r["status"] for r in SD.asked_rows(q)] == ["Flexible"]
    html = RP._fea_limits({"check": {"rows": rows}})
    assert "Flexible" in html and "Met" not in html
    cfd = RP._asked_rows({"asked": rep})
    assert "Flexible" in cfd[0][3] and "Flexible: told" not in cfd[0][3]
    # the tally: a flexible limit (counts only); the ask itself, with the share outside it, said after it
    assert MR.summary(rep) == "Of the 1 thing you asked for: 0 met; 1 flexible limit."
    assert MR.asks_said(rep) == ("Shortest element edge over 3 mm: flexible - 100% of elements (1,000) have an edge "
                                 "shorter than 3 mm (the shortest is 0.118 mm).")


def test_a_flexible_quality_limit_outside_is_told_flexible_with_its_share():
    q = _q(_quality(strictness="flexible"))
    assert MR.asked_rows(q) == []
    told = MR.asked_rows(q, told=True)
    assert [(r["told"], r["status"], r["shown"]) for r in told] == [("target", "Flexible", "1.2% of elements outside")]
    assert MR.summary(q["requirements"]) == "Of the 1 thing you asked for: 0 met; 1 flexible limit."
    assert MR.asks_said(q["requirements"]) == "Aspect ratio under 3: flexible - 1.2% of elements (12) are over 3; the worst is 5.2."


def test_said_flexible_and_inside_is_simply_met():
    for row in (_edge(0, strictness="flexible"), _quality(0, met=True, strictness="flexible")):
        q = _q(row)
        got = MR.asked_rows(q)
        assert [(r["measured"], r["limit"], r["op"]) for r in got] == [(0, 0, "==")]        # a met limit
        assert MR.asked_rows(q, told=True) == got and "told" not in got[0]
        assert MR.summary(q["requirements"]) == "Of the 1 thing you asked for: 1 met."


def test_nothing_said_is_as_before():
    # an edge length: a target, told with its share - "Target, not met", counted an edge-length target
    q = _q(_edge(N))
    assert MR.asked_rows(q) == []
    assert [(r["status"], r["shown"]) for r in MR.asked_rows(q, told=True)] == [
        ("Target, not met", "100% of elements under")]
    assert MR.summary(q["requirements"]) == "Of the 1 thing you asked for: 0 met; 1 edge-length target."
    assert MR.asks_said(q["requirements"]).startswith("Shortest element edge over 3 mm: target, not met - 100% of "
                                                      "elements (1,000) have an edge shorter than 3 mm")
    # a quality limit: held as written - a judged row, not fully met
    q = _q(_quality())
    assert [(r["measured"], "told" in r) for r in MR.asked_rows(q)] == [(12, False)]
    assert MR.summary(q["requirements"]).startswith("Of the 1 thing you asked for: 0 met, 1 not fully met")


def _finer(**extra) -> dict:
    """A size for the whole mesh the part's shape built far finer than asked, as the solid FEA route measures it."""
    row = {"id": "r1", "field": "global", "asked": "global element size 10 mm", **extra}
    got = {"applied": True, "measured": 0.95, "met": False, "delivered": "elements are about 0.95 mm"}
    got.update(MR.finer_global(row, 10.0, 0.95, 13.0))
    return {**row, **got}


def test_a_size_built_far_finer_than_asked_is_told_never_a_met_limit(tmp_path):
    q = _q(_finer())
    assert MR.asked_rows(q) == [] and not [r for r in VL.fea_limits(q) if r["key"].startswith("asked_")]
    told = [(r["told"], r["status"], r["shown"]) for r in MR.asked_rows(q, told=True)]
    assert told == [("finer", "Finer than asked", "about 0.95 mm")]
    assert [(r["ok"], r["status"], r["shown"]) for r in _table(q, tmp_path)] == [
        (None, "Finer than asked", "about 0.95 mm")]
    assert [r["status"] for r in SD.asked_rows(q)] == ["Finer than asked"]
    assert "Finer than asked" in RP._asked_rows({"asked": q["requirements"]})[0][3]
    assert MR.summary(q["requirements"]) == "Of the 1 thing you asked for: 0 met; 1 finer than asked."
    assert MR.asks_said(q["requirements"]).startswith("Global element size 10 mm: finer than asked - elements are "
                                                      "about 0.95 mm, finer than the 10 mm asked")


def test_a_hard_size_built_far_finer_than_asked_is_judged_not_met():
    q = _q(_finer(strictness="hard"))
    assert [(r["measured"], r["limit"], "told" in r) for r in MR.asked_rows(q)] == [(1, 0, False)]
    assert MR.asked_rows(q, told=True) == MR.asked_rows(q)
    assert MR.summary(q["requirements"]).startswith("Of the 1 thing you asked for: 0 met, 1 not fully met")


def test_a_shortest_edge_alone_is_said_as_asked_never_as_a_range():
    sizes = FR.whole_sizes({}, {"sizes": {"min_edge_mm": 3.0}, "rows": []}, ["Part_1"], base_m=0.0202)
    said = P.user_words({"user_sizes": sizes, "zones": [{"id": "P1", "kind": "part", "user": "Part_1",
                                                         "size_m": 0.0202},
                                                        {"id": "R1", "kind": "round", "user": "Part_1",
                                                         "size_m": 0.000118}]})
    assert said[0].startswith("You asked for a shortest edge of 3 mm on the whole mesh: its elements are meshed at "
                              "20.2 mm, and nothing in them finer than 0.118 mm")
    assert "37.4" not in said[0]
    # a global size keeps its band, as before
    sizes = FR.whole_sizes({}, {"sizes": {"global_mm": 10.0}, "rows": []}, ["Part_1"], base_m=0.02)
    assert P.user_words({"user_sizes": sizes, "zones": []})[0].startswith("You asked for 5-15 mm elements on the "
                                                                          "whole mesh")
