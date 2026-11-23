# Responsibility: Verify that every typed ask has its row in the report's "You asked" list, met or not - an edge-length target past its share, a setting, the nearest there is, not applied - told with its status in plain words, never judged and never one of the verdict's limits.
"""m485w round 15: a typed edge range (R1 "elements between 2 and 6 mm" on fea0053, R3, R4, R8 on a shell) was read,
applied and told in the tally, but the report's table carried a row only for an end that was met: R1 one row, R3, R4
and R8 none."""
from __future__ import annotations

import pytest

from meshpipeline.engines import mesh_requirements as MR


def _row(field: str, asked: str, *, met, outside: int = 0, pct: float = 0.0, **kw) -> dict:
    r = {"id": "r1" if field == "min_edge" else "r2", "field": field, "asked": asked, "applied": True, "met": met,
         "outside": outside, "outside_pct": pct, "delivered": "every element is inside it", "excess": 0, **kw}
    if met is None:
        r.update(target=True, delivered=f"{pct:g}% of elements outside it")
    return r


LO, HI = "shortest element edge over 2 mm", "longest element edge under 6 mm"
CASES = {
    "both met": [_row("min_edge", LO, met=True), _row("max_edge", HI, met=True)],
    "one met": [_row("min_edge", LO, met=None, outside=3660, pct=36.6), _row("max_edge", HI, met=True)],
    "neither met": [_row("min_edge", LO, met=None, outside=3660, pct=36.6),
                    _row("max_edge", HI, met=None, outside=120, pct=1.2)],
}


@pytest.mark.parametrize("name", list(CASES))
def test_a_range_has_two_rows_whatever_was_met(name):
    q = {"requirements": {"rows": CASES[name]}}
    rows = MR.asked_rows(q, told=True)
    assert [r["label"] for r in rows] == [f"You asked: {LO}", f"You asked: {HI}"]
    for r, src in zip(rows, CASES[name], strict=True):
        if src["met"] is None:
            assert r["told"] == "target" and r["status"] == "Target, not met" and r["measured"] == 0
            assert r["shown"] == f"{src['outside_pct']:g}% of elements {'under' if src['field'] == 'min_edge' else 'over'}"
        else:
            assert "told" not in r
    # the verdict's limits stay the judged asks only
    assert len(MR.asked_rows(q)) == sum(1 for r in CASES[name] if r["met"] in (True, False))


@pytest.mark.parametrize("name", list(CASES))
def test_on_a_shell_the_range_has_two_rows_in_the_report(name):
    from meshpipeline.engines import shell_delivery as SD
    from meshpipeline.render import mesh_report as RP

    q = {"requirements": {"rows": CASES[name]}}
    rows = SD.asked_rows(q)
    assert len(rows) == 2
    html = RP._fea_limits({"check": {"rows": [{**r, "ok": None if r.get("told") else r["measured"] == 0}
                                              for r in rows]}})
    assert html.count("You asked:") == 2
    if name != "both met":
        assert "Target, not met" in html and "36.6% of elements under" in html


@pytest.mark.parametrize("name", list(CASES))
def test_on_a_solid_the_range_has_two_rows_and_a_target_never_makes_it_unchecked(name, tmp_path):
    import json

    from meshpipeline.engines import fea_results as FR
    q = {"cells": 1000, "fea_inverted": 0, "fea": {"plan": True, "rules": {}}, "requirements": {"rows": CASES[name]}}
    (tmp_path / "quality.json").write_text(json.dumps(q))
    (tmp_path / "fea_plan.json").write_text(json.dumps({"words": [], "faces": [], "hot_spots": []}))
    got = FR.results(tmp_path)
    asked = [r for r in got["check"]["rows"] if str(r.get("label") or "").startswith("You asked")]
    assert len(asked) == 2, got["check"]["rows"]
    # a told row is never judged: the outcome is the one the judged rows alone give (here the fixture's own
    # unmeasured figures decide it, the same with or without the targets)
    (tmp_path / "quality.json").write_text(json.dumps({**q, "requirements": {"rows": CASES["both met"]}}))
    assert got["check"]["outcome"] == FR.results(tmp_path)["check"]["outcome"]
    assert all(r["ok"] is None for r in asked if r.get("told"))


@pytest.mark.parametrize("row,status", [
    ({"id": "g", "field": "growth", "asked": "growth 1.2", "applied": True, "met": None}, "Applied (a setting)"),
    ({"id": "p", "field": "local", "asked": "1.5 mm on the leg", "applied": True, "met": None, "nearest": True},
     "Nearest there is"),
    ({"id": "u", "field": "control:file_units", "asked": "files in mm", "applied": False, "met": None}, "Not applied"),
    ({"id": "w", "field": "control:wall_elements", "asked": "4 through every wall", "applied": True, "met": False,
      "excess": 3, "delivered": "3 through"}, None),
])
def test_every_other_ask_keeps_its_row(row, status):
    rows = MR.asked_rows({"requirements": {"rows": [row]}}, told=True)
    assert len(rows) == 1 and rows[0]["label"] == f"You asked: {row['asked']}"
    assert rows[0].get("status") == status
