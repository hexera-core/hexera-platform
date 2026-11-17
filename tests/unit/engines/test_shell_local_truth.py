# Responsibility: Verify that a size asked in one named place is told truthfully on a shell mesh: no shell step reads it, so its row says "not applied" (it was told "set part by part" and counted as applied before), while a solid keeps its part-by-part line.
# Boundaries: pure over plain data.
from __future__ import annotations

import json

from meshpipeline.engines import mesh_requirements as MR


def _req():
    args = json.dumps({"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "physics": [],
                       "element_kind": [], "limits": [], "other": [],
                       "local": [{"where": "the bracket", "what": "size", "value": 3, "unit": "mm",
                                  "quote": "3 mm on the bracket"}]})
    req, _d, _na = MR.check(args, "3 mm on the bracket", route="fea")
    return req


def test_a_size_in_a_named_place_is_not_applied_on_a_shell_and_is_said():
    row = MR.elements_row("local", _req(), 1000, "shell")
    assert row["applied"] is False and row["delivered"].startswith("not applied - a size in one named place")
    rep = {"rows": [{**r, **MR.elements_row(r["field"], _req(), 1000, "shell")} for r in _req()["rows"]]}
    assert "1 could not be applied to a shell mesh" in MR.summary(rep, kind="shell")


def test_a_solid_still_sets_it_part_by_part():
    row = MR.elements_row("local", _req(), 1000, "solid")
    assert row["applied"] is True and "part by part" in row["delivered"]
