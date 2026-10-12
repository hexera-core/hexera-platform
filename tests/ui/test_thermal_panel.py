# Responsibility: Verify an ECXML upload's check opens as the file's own thermal model in a real
# browser - the parts as the file groups them, coloured as the 3D draws them, picked from either
# side; the air box drawn faint; no stickers, no opening rows; and "Mesh this model" sending only
# the detail level. On dev the set-top box came up with six "outlet" rows at "60% sure".
# Boundaries: the check and the skin are the ones the scout serves, built here from a real ECXML
# file (tests/unit/cad/ecxml_models) by the real converter, placement and thermal view; only the
# network is stood in for.
from __future__ import annotations

import json
import re

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "thermal-session"
#: The generic check's words that have no place on a thermal model's panel (as words or the start
#: of one: "outlet_2", "Sure" - not the "sure" in "pressure").
FORBIDDEN = re.compile(r"(?<![a-z])(?:inlet|outlet|sure|triangle|sticker)", re.I)


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The set-top box's check and skin, as GET /check and /check/skin serve them."""
    from tests.unit.cad.ecxml_models import set_top_box

    from meshpipeline.application import thermal_view as tv
    from meshpipeline.cad.ingest import canonicalise
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_build import read_ecxml_sidecar
    from meshpipeline.cad.ingest.ecxml_place import place, placed_skin

    work = tmp_path_factory.mktemp("thermal")
    src = work / "set_top_box.ecxml"
    src.write_bytes(set_top_box().xml())
    canonical = canonicalise(src, work / "out", stem="geometry", ecxml_form="placed")
    sidecar = read_ecxml_sidecar(canonical.path)
    placement = place(read_ecxml(src))
    view = tv.thermal_view(sidecar, placement, detail=tv.detail_levels(placement))
    check = {"status": "ready", "named": True, "skin": True, "pictures": [], "check_mode": "thermal",
             "proposal": {"part": view["title"], "input_kind": "solid-assembly", "flow": "internal",
                          "openings": [], "size_mm": view["domain"]["size_mm"], "up_axis": "+z",
                          "unit": "mm", "unit_fixed": True, "scale_to_m": 0.001},
             "thermal": view}
    return check, placed_skin(placement)


def _open(live, served, session=SESSION):
    check, skin = served
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__confirmed = null;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(skin)).replace("__CHECK__", json.dumps(check))
       .replace("__S__", session), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{session}']", timeout=90,
                  what="the thermal stage to initialise")


def test_a_thermal_model_opens_as_the_files_own_model(live, served):
    _open(live, served)
    seen = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'], root = document.getElementById('gstage-{SESSION}');
      return {{thermal: h.thermal, parts: h.parts(), fellBack: !!window.__fellBack,
        rows: [...root.querySelectorAll('.tm-row[data-region]')].map(r => r.dataset.region),
        groups: [...root.querySelectorAll('.tm-group')].map(g => g.dataset.group),
        swatch: root.querySelector('.tm-row[data-region="PCB"] .tm-sw').style.background,
        pcb: h.color('PCB'),
        title: root.querySelector('.tm-title').textContent,
        strip: [...root.querySelectorAll('.tm-tile')].map(t => t.querySelector('b').textContent + ' ' + t.querySelector('span').textContent),
        air: root.querySelector('.tm-air').textContent,
        levels: [...root.querySelectorAll('.tm-level')].map(l => [l.dataset.level, l.classList.contains('on'), l.textContent.replace(/\\s+/g, ' ').trim()]),
        fans: root.querySelector('.tm-list')?.textContent || '',
        generic: root.querySelectorAll('.gc-pin, .gc-table, .gc-form, .gc-legend, .gc-kind, .gc-flow').length,
        airbox: h.airbox(), xray: h.xray(), caseOpacity: h.opacity('Case'), probes: h.probes(),
        button: root.querySelector('.tm-mesh').textContent}};
    }})()""")
    assert seen["thermal"] is True and seen["fellBack"] is False
    assert seen["rows"] == ["Case", "PCB", "SoC", "DDR_1", "DDR_2", "PMIC", "Bulk_cap", "SoC_heat_sink"]
    assert sorted(seen["parts"]) == sorted(seen["rows"]) and seen["groups"] == ["Housing", "Mainboard"]
    # the list's swatch is the colour the part is drawn in
    r, g, b = (round(v * 255) for v in seen["pcb"])
    assert seen["swatch"] == f"rgb({r}, {g}, {b})"
    assert seen["title"] == "Set-top box, vented"
    assert seen["strip"] == ["8.7 W total power", "8 parts", "5 materials", "35 °C room air"]
    assert seen["air"] == "180 × 150 × 80 mm, open to the room on all six sides."
    assert [lv[0] for lv in seen["levels"]] == ["draft", "standard", "max"]
    assert [lv[1] for lv in seen["levels"]] == [False, True, False]          # Standard by default
    assert all("cells" in lv[2] for lv in seen["levels"]), seen["levels"]
    assert "Exhaust fan" in seen["fans"] and "blows +x" in seen["fans"]
    # no stickers, no opening rows, no kind or flow questions; the case seen through; the air box faint
    assert seen["generic"] == 0
    assert seen["airbox"] is True and seen["xray"] is True and seen["caseOpacity"] < 0.5
    assert seen["probes"] == 2 and seen["button"] == "Mesh this model"
    assert_clean(live, "the thermal stage")


def test_a_part_picked_on_either_side_is_lit_on_both(live, served):
    _open(live, served)
    h = f"window._vdbg['gstage:{SESSION}']"
    root = f"document.getElementById('gstage-{SESSION}')"
    # a row: the part lit, the rest faded, a box round it
    lit = live.evaluate(f"""(() => {{
      {root}.querySelector('.tm-row[data-region="PMIC"]').click();
      return {{sel: {h}.selected(), pmic: {h}.opacity('PMIC'), pcb: {h}.opacity('PCB'), outline: {h}.outline(),
               row: {root}.querySelector('.tm-row.sel')?.dataset.region}};
    }})()""")
    assert lit == {"sel": "PMIC", "pmic": 1, "pcb": pytest.approx(0.12), "outline": True, "row": "PMIC"}, lit
    # the same row again puts every part back
    back = live.evaluate(f"""(() => {{
      {root}.querySelector('.tm-row[data-region="PMIC"]').click();
      return {{sel: {h}.selected(), pcb: {h}.opacity('PCB'), rows: {root}.querySelectorAll('.tm-row.sel').length}};
    }})()""")
    assert back == {"sel": None, "pcb": 1, "rows": 0}, back
    # a click on the part in the view, made by the browser's own mouse, selects its row
    x, y = live.evaluate(f"{h}.screenOf({h}.centreOf('DDR_1'))")
    assert live.evaluate(f"{h}.pickAt({x}, {y})") == "DDR_1"
    live.click(x, y)
    live.wait_for(f"{h}.selected() === 'DDR_1'", timeout=10, what="the clicked part to be selected")
    row = live.evaluate(f"{root}.querySelector('.tm-row.sel')?.dataset.region")
    assert row == "DDR_1"
    # "Show all" in the view puts them back; a group shows all its parts at once
    shown = live.evaluate(f"""(() => {{
      document.getElementById('gs-all-{SESSION}').click();
      const after = {{sel: {h}.selected(), rows: {root}.querySelectorAll('.tm-row.sel').length}};
      [...{root}.querySelectorAll('.tm-group')].find(g => g.dataset.group === 'Mainboard').querySelector('.tm-only').click();
      return {{after, lit: {h}.lit().length, case: {h}.opacity('Case'), soc: {h}.opacity('SoC')}};
    }})()""")
    assert shown["after"] == {"sel": None, "rows": 0} and shown["lit"] == 7, shown
    assert shown["soc"] == 1 and shown["case"] < 0.12, shown
    assert_clean(live, "picking parts on the thermal stage")


def test_mesh_this_model_sends_only_the_detail_level(live, served):
    _open(live, served)
    live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      root.querySelector('.tm-level[data-level="max"]').click();
      root.querySelector('.tm-mesh').click();
    }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    done = live.evaluate(f"""(() => ({{
      body: window.__confirmed,
      gone: !document.getElementById('gstage-{SESSION}'),
      wb: document.getElementById('app').classList.contains('wb'),
      hook: !!(window._vdbg && window._vdbg['gstage:{SESSION}'])}}))()""")
    # the server knows it is a thermal model from its own stored check and fills the rest
    assert done["body"] == {"detail": "max", "openings": []}, done
    assert done["gone"] is True and done["wb"] is False and done["hook"] is False, done


def test_the_panel_never_speaks_of_openings_roles_or_triangles(live, served):
    check, _ = served
    _open(live, served)
    text = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      root.querySelectorAll('details').forEach(d => d.open = true);          // the build report too
      return [root.textContent, ...[...root.querySelectorAll('[title],[aria-label],[placeholder]')]
        .map(e => [e.title, e.getAttribute('aria-label'), e.getAttribute('placeholder')].join(' '))].join(' ');
    }})()""")
    t = check["thermal"]
    own = set()
    for key in ("tree", "fans", "vents", "resistances", "sources", "compact_models", "probes", "materials"):
        for row in t.get(key) or []:
            own.add(str(row.get("name")))
            own.update(str(p) for p in row.get("path") or [])
    for name in sorted(own, key=len, reverse=True):
        text = text.replace(name, " ")
    assert not FORBIDDEN.search(text), FORBIDDEN.findall(text)
    assert "flows through" not in text and "Proceed" not in text
