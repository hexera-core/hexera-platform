# Responsibility: Verify an FEA part's stage shows EVERY hot spot its measurement found: a part with
# hundreds of them (Areen's F1 car had 208) has a mark on the part for each - small marks without
# numbers past the tag limit - and a line for each in the panel, in one "Hot spots" section that is always
# shown, grouped by type (each type its own line with its count and range, opening on a click);
# clicking a mark opens the folds its line sits in and lights it. A part with a few hot spots keeps its
# numbered tags.
# Boundaries: the skin is built through the worker's own code from a tube made here; the FEA view is
# the server's shape, built here. Only the network is stood in for.
from __future__ import annotations

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
      const sec = root.querySelector('.fea-sec-spots');
      return {{marks: marks.length, small: marks.filter((m) => m.classList.contains('mark')).length,
               rows: rows.length, shown: rows.filter((r) => !r.closest('[hidden]')).length,
               groups: [...root.querySelectorAll('.fea-spot-group')].map((g) =>
                 [g.dataset.type, Number(g.querySelector('.fea-spot-n').textContent)]),
               open: [...root.querySelectorAll('.fea-spot-group.open')].map((g) => g.dataset.type),
               sums: [...root.querySelectorAll('.fea-spot-sum')].map((b) => b.textContent),
               head: [...sec.querySelectorAll('.fea-sec-title > span')].map((e) => e.textContent),
               secOpen: !sec.closest('[hidden]'), wall: root.querySelector('.fea-wall').textContent,
               unsent: !!root.querySelector('.fea-spot-unsent')}}; }})()""")


def test_every_hot_spot_of_a_busy_part_has_a_mark_and_a_line(live):
    sid = "feaspots"
    spots = _spots(KINDS)
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _fea_check(spots))
    s = _state(live, sid)
    assert s["marks"] == 208 and s["small"] == 208          # every one marked, small past the tag limit
    assert s["rows"] == 208 and not s["unsent"]               # and every one listed
    assert s["groups"] == [["sharp_corner", 174], ["notch", 1], ["fillet", 29], ["hole", 4]]
    # ONE SECTION, ALWAYS SHOWN: its header says how many and that they are refined automatically; each type
    # is its own line with its range, and only those folds are closed - no line of the 208 shows until opened
    assert s["head"] == ["Hot spots", "208", "refined automatically"] and s["secOpen"] and s["shown"] == 0
    assert s["sums"] == ["90°, capped", "radius 2 mm", "radius 2 mm", "radius 2 mm"]
    assert s["wall"].startswith("10 mm")                       # the thinnest wall is a key fact, outside the fold
    # a mark clicked on the part opens the folds its line sits in, and lights it
    hidden = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('.fea-spot[data-spot="H200"]').click();
      const row = root.querySelector('.fea-spot-row[data-spot="H200"]');
      return [!!row.closest('[hidden]'), row.classList.contains('sel'),
              root.querySelector('.fea-spot[data-spot="H200"]').classList.contains('sel')]; }})()""")
    assert hidden == [False, True, True]
    s = _state(live, sid)
    assert s["secOpen"] and s["open"] == ["fillet"] and s["shown"] == 29
    # a type's line opens that type whole, and no other; a second click closes it
    grp = f"""document.querySelector('#gstage-{sid} .fea-spot-group[data-type="sharp_corner"] .fea-grp-btn').click()"""
    live.evaluate(grp)
    s = _state(live, sid)
    assert s["shown"] == 174 + 29 and s["open"] == ["sharp_corner", "fillet"]
    live.evaluate(grp)
    assert _state(live, sid)["shown"] == 29
    assert_clean(live, "an FEA stage with 208 hot spots")


def test_a_part_with_a_few_hot_spots_keeps_its_numbered_tags(live):
    sid = "feafew"
    spots = _spots((("hole", "hole", 3), ("fillet", "fillet", 2)))
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _fea_check(spots))
    s = _state(live, sid)
    assert s["marks"] == 5 and s["small"] == 0 and s["rows"] == 5 and s["shown"] == 0
    tags = live.evaluate(f"[...document.querySelectorAll('#gstage-{sid} .fea-spot')].map((m) => m.textContent)")
    assert tags == ["H1", "H2", "H3", "H4", "H5"]
    # each line in a few words: its tag, its kind and size
    live.evaluate(f"""document.querySelector('#gstage-{sid} .fea-spot-group[data-type="hole"] .fea-grp-btn').click()""")
    line = live.evaluate(f"""document.querySelector('#gstage-{sid} .fea-spot-row[data-spot="H1"]').textContent""")
    assert " ".join(line.split()) == "H1hole · r 2 mm"
    assert _state(live, sid)["shown"] == 3
    assert_clean(live, "an FEA stage with 5 hot spots")
