# Responsibility: Verify an FEA assembly's stage lists every pair of parts (PLAN-fea-19/20) with what was measured and a type to pick: each pair follows the whole-assembly answer and the small-gap switch until the user sets it, a pair the user set keeps their choice, an overlap says which part keeps its full shape and lets the user pick the other, and the confirmation carries every pair as it was left.
# Boundaries: the skin is built through the worker's own code from a tube made here; the FEA view is the server's own (application/fea_check.assembly_view) over an assembly record written here. Only the network is stood in for.
from __future__ import annotations

import pytest
from conftest import assert_clean
from test_geometry_stage_open_ends import R_IN, R_OUT, _open, _skin, _tube

pytestmark = pytest.mark.ui

#: Four parts: Part_1-Part_2 touch, Part_1-Part_3 face each other across 0.2 mm, Part_2-Part_4 pass
#: into each other (Part_4 the smaller).
RECORD = {
    "parts": [{"id": f"Part_{k}", "name": n, "faces": 6, "volume_share": v, "volume_mm3": v * 1e4}
              for k, n, v in ((1, "Base", 0.5), (2, "Bracket", 0.3), (3, "Boss", 0.15), (4, "Pin", 0.05))],
    "interfaces": [
        {"id": "Interface_1", "kind": "touching", "parts": ["Part_1", "Part_2"], "area_mm2": 400.0, "faces": {}},
        {"id": "Interface_2", "kind": "gap", "parts": ["Part_1", "Part_3"], "area_mm2": 60.0, "gap_mm": 0.2,
         "gap_max_mm": 0.21, "reach_mm": 0.26, "faces": {}},
        {"id": "Interface_3", "kind": "overlap", "parts": ["Part_2", "Part_4"], "overlap_share": 0.18,
         "depth_mm": 0.9, "faces": {}},
    ],
    "free_parts": [], "notes": []}


def _check() -> dict:
    from meshpipeline.application import fea_check
    from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
    from meshpipeline.cad.fea_measure import FACT_KEY

    view = fea_check.fea_view({"solids": 4, "source_format": "step", FACT_KEY: {ASM_KEY: RECORD}})
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "assembly", "input_kind": "body-surface", "size_mm": [300.0, 100.0, 100.0], "unit": "mm",
        "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []}, "fea": view}


def _rows(live, sid: str) -> list:
    return live.evaluate(f"""[...document.querySelectorAll('#gstage-{sid} .fea-pair')].map((r) => [
      r.dataset.key, r.querySelector('.fea-pair-type').value, r.querySelector('.fea-pair-what').textContent.trim(),
      r.querySelector('.fea-pair-why').textContent.trim(),
      r.querySelector('.fea-pair-keep') ? r.querySelector('.fea-pair-keep').value : null])""")


def _set(live, sid: str, selector: str, value) -> None:
    live.evaluate(f"""(() => {{
      const el = document.querySelector('#gstage-{sid} {selector}');
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = {str(bool(value)).lower()};
      else el.value = {value!r};
      el.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")


def test_every_pair_is_listed_set_one_by_one_and_confirmed(live):
    sid = "feapairs"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check())
    rows = _rows(live, sid)
    assert [r[0] for r in rows] == ["Part_1|Part_2", "Part_1|Part_3", "Part_2|Part_4"]
    assert [r[1] for r in rows] == ["bonded", "bonded", "bonded"]
    assert "touch over 400 mm²" in rows[0][2] and "sit 0.2 mm apart" in rows[1][2]
    assert "pass into each other (18% of the smaller part, about 0.9 mm deep)" in rows[2][2]
    # the smaller body keeps its full shape where they pass into each other
    assert rows[2][4] == "Part_4"
    # the user sets one pair; the whole-assembly answer then moves only the others
    _set(live, sid, '.fea-pair[data-key="Part_1|Part_2"] .fea-pair-type', "off")
    _set(live, sid, 'input[name="fea-connection"][value="contact"]', True)
    rows = _rows(live, sid)
    assert [r[1] for r in rows] == ["off", "contact", "contact"]
    assert rows[0][3] == "Set by you for this pair." and "Contact, the answer for the whole assembly" in rows[1][3]
    # the small-gap switch off: the small-gap pair is Off, said why
    _set(live, sid, 'input[name="fea-gap-joins"]', False)
    rows = _rows(live, sid)
    assert rows[1][1] == "off" and "switched off" in rows[1][3]
    # the other part keeps the overlap, as the user says
    _set(live, sid, '.fea-pair[data-key="Part_2|Part_4"] .fea-pair-keep', "Part_2")
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=30, what="the confirmation to be sent")
    body = live.evaluate(f"window.__confirmed['{sid}']")
    assert body["fea_connection"] == "contact" and body["fea_gap_joins"] is False
    assert body["fea_pairs"] == {"Part_1|Part_2": {"type": "off"}, "Part_1|Part_3": {"type": "off"},
                                 "Part_2|Part_4": {"type": "contact", "keep": "Part_2"}}
    assert_clean(live, "an FEA assembly's pair list")
