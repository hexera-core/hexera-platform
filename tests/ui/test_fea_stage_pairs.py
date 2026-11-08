# Responsibility: Verify an FEA assembly's stage lists every pair of parts (PLAN-fea-19/20) with what was measured and a type to pick: each pair follows the whole-assembly answer and the small-gap switch until the user sets it, a pair the user set keeps their choice, an overlap says which part keeps its full shape and lets the user pick the other, and the confirmation carries every pair as it was left.
# Boundaries: the skin is built through the worker's own code from a tube made here; the FEA view is the server's own (application/fea_check.assembly_view) over an assembly record written here. Only the network is stood in for.
from __future__ import annotations

import json

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
    # by pair key: the small gaps sit under their own switch, apart from the other joins
    return sorted(live.evaluate(f"""[...document.querySelectorAll('#gstage-{sid} .fea-pair')].map((r) => [
      r.dataset.key, r.querySelector('.fea-pair-type').value, r.querySelector('.fea-pair-what').textContent.trim(),
      r.querySelector('.fea-pair-why').textContent.trim(),
      r.querySelector('.fea-pair-keep') ? r.querySelector('.fea-pair-keep').value : null])"""))


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


#: The same assembly, as a shell run's measurement: each joint marked on the part, the fuse's notes (one of them
#: the small-gap list again), the first part named like a body in a STEP assembly, and a file name.
def _busy_check(file_name: str = "frame assembly") -> dict:
    import copy

    from test_geometry_stage_open_ends import ORIGIN

    from meshpipeline.application import fea_check
    from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
    from meshpipeline.cad.fea_measure import FACT_KEY

    rec = copy.deepcopy(RECORD)
    ox, oy, oz = (1000 * v for v in ORIGIN)
    for k, i in enumerate(rec["interfaces"]):
        i["at_mm"] = [ox + 60.0 * (k + 1), oy + 1000 * R_OUT, oz]
    rec["parts"][0]["name"] = "Bolt M8x35 hex head_A_1"
    rec["joins"] = {"notes": ["1 pair(s) of parts face each other across a small gap and are joined there, as a weld "
                              "or a thread the CAD does not draw (up to 0.5 mm, less on small parts): Bolt and Boss.",
                              "The parts' surfaces were not all measured in the time set for it: where they meet is as "
                              "the fuse found it."]}
    facts = {"solids": 4, "source_format": "step", "file_name": file_name, FACT_KEY: {ASM_KEY: rec},
             "validity": {"clean": True, "summary": "The geometry is clean."}}
    view = fea_check.fea_view(facts)
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "Bolt M8x35 hex head_A_1", "input_kind": "body-surface", "size_mm": [300.0, 100.0, 100.0],
        "unit": "mm", "unit_basis": "file_declared", "notes": [], "openings": [], "faces": [], "holes": []}, "fea": view}


def test_the_assembly_panel_is_folded_named_for_its_file_and_lists_each_gap_once(live):
    sid = "feaasmfold"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _busy_check())
    s = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      const sec = root.querySelector('.fea-sec-asm');
      const text = root.querySelector('.fea-panel').textContent;
      return {{title: root.querySelector('.fea-title').textContent, tag: root.querySelector('.fea-tag').textContent,
               folds: [...root.querySelectorAll('.fea-sec')].map((w) => [w.dataset.fold, w.classList.contains('open'),
                 w.querySelector(':scope > .fea-fold-body').hidden]),
               head: sec.querySelector('.fea-sec-sum').textContent,
               seg: [...sec.querySelectorAll('.fea-sec-head input[name="fea-connection"]')].map((r) => [r.value, r.checked]),
               gapRows: [...root.querySelectorAll('.fea-gap-rows .fea-pair')].map((r) => r.dataset.key),
               joinRows: [...root.querySelectorAll('.fea-itfs:not(.fea-gap-rows) .fea-pair')].map((r) => r.dataset.key),
               gapLists: root.querySelectorAll('.fea-gap-list').length,
               gapBoxes: root.querySelectorAll('input[name="fea-gap-joins"]').length,
               bossMentions: [...root.querySelectorAll('.fea-pair-name, .fea-gap-rows li, .fea-line')]
                 .filter((e) => e.textContent.includes('Boss')).length, fuseNote: text.includes('across a small gap'),
               timeNote: text.includes('not all measured'), clean: root.querySelector('.fea-clean').textContent.trim(),
               cleanBtn: !!root.querySelector('.fea-clean-btn'),
               hint: root.querySelector('.gc-unit-hint').dataset.sure,
               reachShown: [...root.querySelectorAll('.fea-pair-reach')].filter((r) => !r.closest('[hidden]')).length,
               question: !!root.querySelector('.fea-q')}}; }})()""")
    # THE TITLE IS THE FILE'S, never the first body's; the purpose beside it
    assert s["title"] == "frame assembly" and "Bolt M8x35" not in s["title"] and s["tag"] == "FEA"
    # every fold closed at first, its header one line: how many parts and joins, and the one answer as a switch
    assert s["folds"] == [["asm", False, True]]
    assert s["head"] == "4 parts · 3 joins" and s["seg"] == [["bonded", True], ["contact", False]]
    # THE SMALL GAPS LISTED ONCE: the gap pair under its one switch, the others apart; no second list, and the
    # fuse's note that says the same gaps again is left out (the note about timing stays, folded)
    assert s["gapRows"] == ["Part_1|Part_3"] and s["joinRows"] == ["Part_1|Part_2", "Part_2|Part_4"]
    assert s["gapLists"] == 0 and s["gapBoxes"] == 1 and not s["fuseNote"] and s["timeNote"]
    assert s["bossMentions"] == 2                       # its one gap row (its line and that row), no other line
    # the clean check in one line; the reach tucked behind each pair; no question paragraph
    assert s["clean"] == "✓Clean" and not s["cleanBtn"] and s["reachShown"] == 0 and not s["question"]
    # a unit the file states needs no line under the box
    assert s["hint"] == "1"
    # a join's mark clicked on the part opens the fold and lights its row; its line opens what is behind it
    got = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('.fea-spot.fea-itf[data-spot="I2"]').click();
      const row = root.querySelector('.fea-pair[data-spot="I2"]');
      const before = [root.querySelector('.fea-sec-asm').classList.contains('open'), !!row.closest('[hidden]'),
                      row.classList.contains('sel'), row.querySelector('.fea-pair-more').hidden];
      row.querySelector('.fea-pair-name').click();
      return before.concat([row.querySelector('.fea-pair-more').hidden,
                            !row.querySelector('.fea-pair-reach').closest('[hidden]')]); }})()""")
    assert got == [True, False, True, True, False, True]
    assert_clean(live, "a folded FEA assembly panel")


def test_an_assembly_with_no_file_name_is_your_part(live):
    sid = "feaasmnoname"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _busy_check(file_name=""))
    assert live.evaluate(f"document.querySelector('#gstage-{sid} .fea-title').textContent") == "Your part"


def test_an_assembly_measured_later_lands_as_its_own_fold_and_keeps_what_was_open(live):
    sid = "feaasmlate"
    full = _busy_check()
    early = dict(full, fea={k: v for k, v in full["fea"].items() if k != "assembly"})
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), early)
    assert live.evaluate(f"document.querySelectorAll('#gstage-{sid} .fea-assembly').length") == 0
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(full)})")
    s = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      const asm = root.querySelector('.fea-assembly');
      return [!!asm.closest('.fea-facts'), asm.nextElementSibling.classList.contains('gc-actions'),
              root.querySelectorAll('.fea-pair').length]; }})()""")
    assert s == [False, True, 3]                     # after the key facts, before Proceed - never among the facts
    # opened, it stays open when a later view says more
    live.evaluate(f"document.querySelector('#gstage-{sid} .fea-sec-asm .fea-sec-btn').click()")
    more = json.loads(json.dumps(full))
    more["fea"]["assembly"]["notes"] = ["one more note"]
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(more)})")
    assert live.evaluate(f"document.querySelector('#gstage-{sid} .fea-sec-asm').classList.contains('open')")
    assert_clean(live, "an assembly measured after the stage opened")
