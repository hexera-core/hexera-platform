# Responsibility: Verify SHMESH-26 on a delivered shell: while the user's own quality limits are in force the shell is judged by them alone - the verdict rows, the result's check, the header's worst-quality figure and the reviewer's facts - with our usual shell targets a quiet "also measured" line and Nastran's own limits still in force; with no notes the result is the one made before.
# Boundaries: pure over a quality record and a workspace folder; no mesh is made, no model is asked.
from __future__ import annotations

import json

import numpy as np

from meshpipeline.contracts.decision_record import limit_met
from meshpipeline.engines import fea_review
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_delivery as SD
from meshpipeline.engines.verdict_limits import fea_limits

JAC = "Jacobian > 0.6 (1% between 0.6 and 0.4 is acceptable)"
#: a welded column's shell as the result screen showed it: every figure inside Nastran's own limits, some past our
#: usual targets (skew 35.8 under 45, quad corners 40.84 and 149.8, Jacobian ratio 0.517 under 0.6)
FIGURES = {"jacobian_ratio": 0.517, "aspect": 3.1, "warp_deg": 4.0, "skew_deg": 35.8, "taper": 0.3,
           "min_angle_quad": 40.84, "max_angle_quad": 149.8, "min_angle_tria": 31.0, "max_angle_tria": 110.0}


def _quality(with_notes: bool) -> dict:
    q = SD.quality_of({"elements": 200, "nodes": 220, "walls_mm": [4.0], "faces": [],
                       "quality": {"quads": 200, "trias": 0, "invalid": 0, "within_nastran": True, "nastran_past": {},
                                   "figures": {k: {"worst": v, "median": v} for k, v in FIGURES.items()}}})
    if with_notes:
        req, dropped, _na = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "element_kind": [], "other": [],
                                                 "limits": [{"measure": "jacobian", "shape": "any", "direction": "min",
                                                             "target": 0.6, "allowed_percent": 1, "outer": 0.4,
                                                             "has_outer": True, "quote": JAC}]}), JAC)
        assert dropped == []
        lim = req["limits"][0]
        values = np.full(200, 0.9)
        values[0] = 0.517                                      # one element in the 1% band, none past 0.4
        row = {"id": lim["id"], "field": "limit:jacobian", "asked": req["rows"][0]["asked"],
               **MR.limit_row(lim, values, 200)}
        q["requirements"] = MR.finish_report([row], 200, "definitions")       # as the shell record keeps it
    return q


def test_with_the_users_limits_in_force_nothing_of_ours_is_past_a_limit():
    q = _quality(True)
    rows = fea_limits(q)                                       # what the verdict and the chat read
    assert [r["key"] for r in rows] == ["shell_nastran_past", "asked_r1"]
    assert all(limit_met(r["measured"], r["op"], r["limit"]) for r in rows)
    assert SD.also_measured(q) == (
        "Also measured against our usual shell targets, not judged while your own limits are in force: skew 35.8 (our "
        "target at least 45); smallest corner angle, quads 40.84 (our target at least 45); largest corner angle, quads "
        "149.8 (our target at most 135); worst element shape 0.517 (our target at least 0.6).")


def test_the_result_is_judged_by_the_users_line_and_the_header_is_neutral(tmp_path):
    q = _quality(True)
    block = SD.results(tmp_path, q, {})
    check = block["check"]
    assert check["outcome"] == "pass" and not any(r["ok"] is False for r in check["rows"])
    assert check["words"] == "Passed: every limit your notes set is met, and every element is inside Nastran's own checks."
    head = next(r for r in check["rows"] if r["key"] == SD.WORST_KEY)
    # the header's figure: the worst element by the user's Jacobian, against their outer limit, met - not amber
    assert (head["measured"], head["limit"], head["ok"]) == (0.517, 0.4, True)
    assert head["words"] == ("Jacobian over 0.6 (up to 1% of elements between 0.4 and 0.6): met - 0.5% of elements "
                             "(1) are under 0.6, none under 0.4; the worst is 0.517.")
    assert [r["key"] for r in check["rows"]].count(SD.WORST_KEY) == 1
    assert not any(r["key"] == "asked_r1" for r in check["rows"])          # listed once, as the header's row
    assert any(w.startswith("Also measured against our usual shell targets") for w in block["plan_words"])
    assert "your own" in check["solvers"]
    # the reviewer is told the same
    (tmp_path / "quality.json").write_text(json.dumps(q))
    facts = fea_review.shell_facts_text(tmp_path)
    assert "QUALITY IS JUDGED ONLY BY THE USER'S OWN LIMITS" in facts and "the worst is 0.517" in facts


def test_with_no_notes_the_result_is_the_one_made_before(tmp_path):
    q = _quality(False)
    rows = fea_limits(q)
    assert [r["key"] for r in rows] == ["shell_aspect", "shell_warp_deg", "shell_skew_deg", "shell_taper",
                                        "shell_min_angle_quad", "shell_max_angle_quad", "shell_min_angle_tria",
                                        "shell_max_angle_tria", "shell_jacobian_ratio", "shell_nastran_past"]
    block = SD.results(tmp_path, q, {})
    head = next(r for r in block["check"]["rows"] if r["key"] == SD.WORST_KEY)
    assert (head["measured"], head["limit"], head["ok"]) == (0.517, 0.6, False)
    assert block["check"]["outcome"] == "concerns"
    assert block["check"]["words"] == "Passed, with notes: the shell is valid; some figures are past a limit (listed)."
    assert SD.also_measured(q) == "" and not any("Also measured" in w for w in block["plan_words"])
    (tmp_path / "quality.json").write_text(json.dumps(q))
    assert "USER'S OWN LIMITS" not in fea_review.shell_facts_text(tmp_path)


def test_the_closing_line_counts_every_line_the_user_wrote():
    rows = [{"field": "global", "asked": "global element size 10 mm", "met": True},
            {"field": "min_edge", "asked": "shortest edge over 5 mm", "met": None, "target": True, "outside": 36,
             "outside_pct": 0.57},
            {"field": "max_edge", "asked": "longest edge under 15 mm", "met": None, "target": True, "outside": 1,
             "outside_pct": 0.016},
            *[{"field": f"limit:{m}", "asked": m, "met": True} for m in "abcdefg"]]
    said = MR.summary({"rows": rows})
    assert said == ("Of the 10 things you asked for: 8 met; 2 edge-length targets: 0.57% of elements have an edge "
                    "shorter than you asked, 0.016% of elements have an edge longer than you asked.")
