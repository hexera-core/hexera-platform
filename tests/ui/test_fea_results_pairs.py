# Responsibility: Verify a delivered FEA assembly's result screen names an OPEN bonded pair in red, with the pair and the way on (PLAN-fea-21), lists every pair as it was joined (PLAN-fea-19/20), and shows no warning when every tied pair is closed.
# Boundaries: the assembly block is the server's own (engines/fea_results.assembly_block) over an adapter record shaped as engines/gmsh/fea_adapter writes it; the panel is the viewer's own module (viewer/fea_results.js) in a real browser. Only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

#: Three parts tied apart (a contact pair elsewhere makes the bonded pairs ties): Part_1-Part_2 tied
#: and closed, Part_1-Part_3 a 0.2 mm gap tied with a reach set far too short, Part_2-Part_3 contact.
JOINTS = [
    {"id": "Interface_1", "key": "Part_1|Part_2", "parts": ["Part_1", "Part_2"], "kind": "touching",
     "type": "bonded", "default": "bonded", "source": "rule", "why": "they touch: Bonded, the answer for the whole assembly"},
    {"id": "Interface_2", "key": "Part_1|Part_3", "parts": ["Part_1", "Part_3"], "kind": "gap", "type": "bonded",
     "default": "bonded", "source": "rule", "reach_mm": 0.001,
     "why": "they face each other across 0.2 mm, inside the small-gap limit: Bonded, the answer for the whole assembly"},
    {"id": "Interface_3", "key": "Part_2|Part_3", "parts": ["Part_2", "Part_3"], "kind": "touching",
     "type": "contact", "default": "bonded", "source": "user", "why": "set to Contact on the stage, for this pair"},
]
OPEN = {"name": "Tie2", "pair": "Interface_2", "parts": ["Part_1", "Part_3"], "reach_m": 1e-6, "reach_from": "set",
        "needed_m": 2.1e-4, "faces": [{"face": "Part_3_Face_4", "nearest_m": 2.0e-4}], "tied_share": 0.0}


def _asm(open_rows: list) -> dict:
    return {"connection": "bonded", "mode": "tie",
            "parts": [{"id": f"Part_{k}", "name": n, "elements": e} for k, n, e in
                      ((1, "Base", 12000), (2, "Bracket", 8000), (3, "Boss", 3000))],
            "joints": [{"name": "Tie1", "kind": "tie", "slave": "Tie1_Part_2", "master": "Tie1_Part_1", "parts": [1, 2]},
                       {"name": "Tie2", "kind": "tie", "slave": "Tie2_Part_3", "master": "Tie2_Part_1", "parts": [1, 3]},
                       {"name": "Contact1", "kind": "contact", "slave": "Contact1_Part_3", "master": "Contact1_Part_2",
                        "parts": [3, 2]}],
            "pairs": JOINTS, "joint_counts": {"bonded": 2, "contact": 1, "off": 0, "edited": 1},
            "bonded_check": {"checked": 2, "closed": 2 - len(open_rows)}, "open_bonded": open_rows}


def _render(live, block: dict, host: str) -> dict:
    return live.evaluate("""(async () => {
      const { feaResultsHtml } = await import('/static/js/viewer/fea_results.js');
      let el = document.getElementById('__HOST__');
      if (!el) { el = document.createElement('div'); el.id = '__HOST__'; document.getElementById('stage').appendChild(el); }
      el.innerHTML = feaResultsHtml('__HOST__', {summary: 'lab', assembly: __BLOCK__, check: {outcome: 'pass', rows: []},
                                                 plan_words: [], hot_spots: [], faces: []});
      const alert = el.querySelector('.fe-open');
      return {alert: alert ? alert.innerText : null, role: alert ? alert.getAttribute('role') : null,
              colour: alert ? getComputedStyle(alert.querySelector('b')).color : null,
              border: alert ? getComputedStyle(alert).borderTopColor : null,
              pairs: [...el.querySelectorAll('.fe-pair')].map((li) => [li.className, li.textContent]),
              summary: el.querySelector('.fe-asm .tr-sum').textContent};
    })()""".replace("__HOST__", host).replace("__BLOCK__", json.dumps(block)), timeout=30)


def _reddish(css: str) -> bool:
    nums = [float(x) for x in css[css.index("(") + 1:css.index(")")].split(",")[:3]]
    return nums[0] > 150 and nums[0] > 1.5 * nums[1] and nums[0] > 1.5 * nums[2]


def test_an_open_bonded_pair_is_named_in_red_and_a_closed_run_is_quiet(live):
    from meshpipeline.engines.fea_results import assembly_block

    got = _render(live, assembly_block(_asm([OPEN])), "fea-open-run")
    assert got["alert"] and got["role"] == "alert"
    assert got["alert"].startswith("A bonded pair is open")
    # the pair, the face, the reach it was given and the way on, in the server's own words
    assert "Tie2 (Part_1 and Part_3)" in got["alert"] and "Part_3_Face_4" in got["alert"]
    assert "switch this pair to Contact or Off on the stage, or set its reach to at least" in got["alert"]
    assert _reddish(got["colour"]) and _reddish(got["border"]), (got["colour"], got["border"])
    # every pair as it was joined, the one the user set among them
    assert [c for c, _t in got["pairs"]] == ["fe-pair bonded", "fe-pair bonded", "fe-pair contact"]
    assert "Part_2 and Part_3: Contact - set to Contact on the stage" in got["pairs"][2][1]
    assert "Pairs: 2 bonded, 1 contact, 0 off (1 set on the stage)." in got["summary"]
    quiet = _render(live, assembly_block(_asm([])), "fea-closed-run")
    assert quiet["alert"] is None
    assert_clean(live, "an FEA assembly's result screen with an open bonded pair")
