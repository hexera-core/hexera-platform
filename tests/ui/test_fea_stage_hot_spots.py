# Responsibility: Verify an FEA part's stage shows EVERY hot spot its measurement found: a part with
# hundreds of them (Areen's F1 car had 208) has a mark on the part for each - small marks without
# numbers past the tag limit - and a line for each in the panel, grouped by type with a count; a
# group's "Show all N" opens the rest of it, and clicking a mark opens and lights its line. A part
# with a few hot spots keeps its numbered tags.
# Boundaries: the skin is built through the worker's own code from a tube made here; the FEA view is
# the server's shape, built here. Only the network is stood in for.
from __future__ import annotations

import json
import math

import pytest
from conftest import assert_clean
from test_geometry_stage_open_ends import LENGTH, ORIGIN, R_IN, R_OUT, _open, _skin, _tube

pytestmark = pytest.mark.ui

KINDS = (("sharp_corner", "inside corner", 174), ("notch", "notch", 1), ("fillet", "fillet", 29), ("hole", "hole", 4))


def _spots(n_by_kind) -> list[dict]:
    """Hot spots spread along the tube's outer wall, sharpest first, the way the server sends them."""
    out, i = [], 0
    total = sum(n for _t, _s, n in n_by_kind)
    ox, oy, oz = ORIGIN
    for t, shape, n in n_by_kind:
        for _ in range(n):
            i += 1
            a = 2 * math.pi * i / total
            at = [1000 * (ox + LENGTH * i / (total + 1)), 1000 * (oy + R_OUT * math.cos(a)), 1000 * (oz + R_OUT * math.sin(a))]
            h = {"id": f"H{i}", "type": t, "shape": shape, "at_mm": at, "faces": [f"Face_{i}"],
                 "capped": t == "sharp_corner"}
            h.update({"angle_deg": 90.0} if t == "sharp_corner" else {"radius_mm": 2.0})
            out.append(h)
    return out


def _fea_check(spots: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for h in spots:
        counts[h["type"]] = counts.get(h["type"], 0) + 1
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "tube", "input_kind": "body-surface", "size_mm": [300.0, 100.0, 100.0], "unit": "mm",
        "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []},
        "fea": {"question": "Is this your part and are the units right?", "bodies": 1, "clean": True,
                "clean_summary": "watertight", "single_part_only": "", "can_proceed": True,
                "measure": {"measured": True, "basis": "cad", "thickness_min_mm": 10.0, "thickness_face": "Face_1",
                            "hot_spots": spots, "counts": counts, "more": 0, "summary": "many hot spots",
                            "notes": []}}}


def _state(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      const marks = [...root.querySelectorAll('.fea-spot')];
      const rows = [...root.querySelectorAll('.fea-spot-row')];
      return {{marks: marks.length, small: marks.filter((m) => m.classList.contains('mark')).length,
               rows: rows.length, shown: rows.filter((r) => !r.hidden).length,
               groups: [...root.querySelectorAll('.fea-spot-group')].map((g) =>
                 [g.dataset.type, Number(g.querySelector('.fea-spot-n').textContent)]),
               opens: [...root.querySelectorAll('.fea-spot-open')].map((b) => b.textContent),
               unsent: !!root.querySelector('.fea-spot-unsent')}}; }})()""")


def test_every_hot_spot_of_a_busy_part_has_a_mark_and_a_line(live):
    sid = "feaspots"
    spots = _spots(KINDS)
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _fea_check(spots))
    s = _state(live, sid)
    assert s["marks"] == 208 and s["small"] == 208          # every one marked, small past the tag limit
    assert s["rows"] == 208 and not s["unsent"]               # and every one listed
    assert s["groups"] == [["sharp_corner", 174], ["notch", 1], ["fillet", 29], ["hole", 4]]
    assert s["opens"] == ["Show all 174", "Show all 29"]
    assert s["shown"] == 8 + 1 + 8 + 4
    # a mark past its group's first lines: clicking it opens and lights its line
    hidden = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('.fea-spot[data-spot="H200"]').click();
      const row = root.querySelector('.fea-spot-row[data-spot="H200"]');
      return [row.hidden, row.classList.contains('sel'),
              root.querySelector('.fea-spot[data-spot="H200"]').classList.contains('sel')]; }})()""")
    assert hidden == [False, True, True]
    # "Show all" opens its own group whole, and no other
    live.evaluate(f"""document.querySelector('#gstage-{sid} .fea-spot-group[data-type="sharp_corner"] .fea-spot-open').click()""")
    s = _state(live, sid)
    assert s["shown"] == 174 + 1 + 9 + 4 and s["opens"] == ["Show all 29"]
    assert_clean(live, "an FEA stage with 208 hot spots")


def test_a_part_with_a_few_hot_spots_keeps_its_numbered_tags(live):
    sid = "feafew"
    spots = _spots((("hole", "hole", 3), ("fillet", "fillet", 2)))
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _fea_check(spots))
    s = _state(live, sid)
    assert s["marks"] == 5 and s["small"] == 0 and s["rows"] == 5 and s["opens"] == []
    tags = live.evaluate(f"[...document.querySelectorAll('#gstage-{sid} .fea-spot')].map((m) => m.textContent)")
    assert tags == ["H1", "H2", "H3", "H4", "H5"]
    assert_clean(live, "an FEA stage with 5 hot spots")


#   -- the stage that GREW its FEA panel marks them too ------------------------------------------
#
# There are two ways onto the FEA panel. One is openFeaStage, which builds the panel and then the
# scene, so initScene draws the marks from p.hot_spots the panel has just set. The other is a stage
# opened on the part alone, before the chat said what the mesh is for, which grows the panel where
# its waiting note stood - and there the SCENE IS OLDER THAN THE PANEL.


def _unrouted(check: dict) -> dict:
    """The same part one poll earlier: measured and drawn, with `route` the empty string the
    server sends until the user's first answer settles what the mesh is for."""
    d = {k: v for k, v in check.items() if k != "fea"}
    d.update(status="scouted", named=False, route="", preview="running")
    return d


def test_the_marks_land_when_the_route_and_the_measurement_arrive_on_one_poll(live):
    """THE ORDINARY ORDERING, not a rare one: the naming asks no model for an FEA part
    (application/geometry_check.py:1768), so its measurement is ready on the poll the route lands
    on. growForm called only scene.rebind(), which touches neither the marks nor the panel's rows,
    and the panel's own follower will not do it either on this payload - followMeasure compares the
    `data-state` feaPanelHtml has just written from that same payload (render/fea_form.js:125,
    :207), so it reports no change and never reaches setSpots.

    MEASURED in Chrome on the unfixed tree: spots [], 0 marks against 2 hot-spot rows listed in
    the panel, and pointing at a row lit nothing. Straight through openFeaStage, which is what
    every other row here drives and what HEAD always did, the same payload gives [h1, h2]."""
    sid = "feagrow"
    spots = _spots((("hole", "hole", 2), ("fillet", "fillet", 1)))
    check = _fea_check(spots)
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _unrouted(check))
    waiting = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      return {{waiting: root.classList.contains('gc-waiting'),
               marks: root.querySelectorAll('.fea-spot').length,
               rows: root.querySelectorAll('.fea-spot-row').length}}; }})()""")
    assert waiting == {"waiting": True, "marks": 0, "rows": 0}, waiting

    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(check)})")
    live.wait_for(f"document.querySelectorAll('#gstage-{sid} .fea-spot-row').length === 3",
                  timeout=60, what="the FEA panel to be built where the waiting note stood")
    live.settle()
    s = _state(live, sid)
    assert s["rows"] == 3, s                      # listed in the panel, as they always were
    assert s["marks"] == 3 and s["small"] == 0, s  # ...and MARKED on the part
    assert live.evaluate(f"window._vdbg['gstage:{sid}'].spots()") == ["H1", "H2", "H3"]
    # the binding setSpots makes, and the only thing the rows are for: a row lights its mark
    lit = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('.fea-spot-row[data-spot="H2"]').dispatchEvent(new MouseEvent('mouseenter'));
      return [...root.querySelectorAll('.fea-spot')].map((m) => [m.dataset.spot,
                m.classList.contains('sel')]); }})()""")
    assert lit == [["H1", False], ["H2", True], ["H3", False]], lit
    assert_clean(live, "an FEA panel grown where the waiting note stood")
