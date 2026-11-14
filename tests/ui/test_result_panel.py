# Responsibility: Verify the short result screen (revamp 5/5) in a real browser - one verdict line, at most four key numbers, the downloads with Everything first, the solver files, ParaView, the Mesh report, then the small reports, Notes and Hot spots each one folded list grouped by kind (348 hot spots are four rows, never 348), a kind that points at its places on the part, and a Mesh report that downloads from its route.
# Boundaries: the surface is the backend's own (three unit hexes) with the FEA block a delivered assembly carries; only the network is stood in for. It asserts what the page holds, never pixels.
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import assert_clean
from test_heatmap import _payload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "render"))
from test_mesh_report import _fea_payload  # noqa: E402

pytestmark = pytest.mark.ui

NOTES = ["2 element(s) at a cusp in the CAD, set aside from the quality floor - the material runs to nothing "
         "here; round it off in CAD: measured (12.5, -3, 40) mm, (14.25, -3, 40) mm, limit none"]
FILES = [{"id": "", "type": "mesh", "label": "Nastran bulk data (.bdf)", "size": 2000, "url": "#"},
         {"id": "", "type": "mesh_bundle", "label": "FEA mesh (.tar.gz)", "size": 9000, "url": "#"}]


def _open(live, job: str, surf: dict, opts: dict) -> None:
    live.evaluate("""(async () => {
      const PAYLOAD = __P__;
      window.__reportAsks = 0;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b), {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__J__/surface')) return json(PAYLOAD);
        if (s.includes('/simulation/__J__/report.html')) { window.__reportAsks += 1;
          return Promise.resolve(new Response('<!DOCTYPE html><title>Mesh report</title>', {status: 200, headers: {'Content-Type': 'text/html'}})); }
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__J__', document.getElementById('stage'), __O__);
      document.getElementById('viewer-__J__').scrollIntoView();
    })()""".replace("__P__", json.dumps(surf)).replace("__J__", job).replace("__O__", json.dumps(opts)), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{job}:tools'] && document.querySelector('#v-keys-{job} dl')",
                  timeout=90, what="the result screen")


def _read(live, job: str) -> dict:
    return live.evaluate(f"""(() => {{
      const bar = document.querySelector('#viewer-{job} .v-bar');
      const drops = [...bar.querySelectorAll('details.rp-drop')];
      return {{
        verdict: bar.querySelector('.rp-verdict').innerText.trim(),
        verdictCls: bar.querySelector('.rp-verdict').className,
        keys: [...bar.querySelectorAll('.rp-keys dt')].map(d => d.textContent),
        downloads: [...bar.querySelectorAll('.v-dl .v-dlbtn b')].map(b => b.textContent),
        drops: drops.map(d => [d.querySelector('summary').innerText.replace(/\\s+/g, ' ').trim(), d.open]),
        spotRows: bar.querySelectorAll('.rp-spots .rp-spotkind').length,
        spotKinds: [...bar.querySelectorAll('.rp-spots .rp-spotkind b')].map(b => b.textContent),
        noteGroups: [...bar.querySelectorAll('.rp-notes .rp-gh')].map(g => g.textContent),
        noteText: (bar.querySelector('.rp-notes .rp-body') || {{}}).textContent || '',
        alert: !!bar.querySelector('.fe-open[role=alert]'),
        faces: (bar.querySelector('.fe-faces-short') || {{}}).textContent || '',
      }}; }})()""")


def test_an_fea_result_is_a_verdict_four_numbers_the_downloads_and_two_folded_lists(live, tmp_path):
    surf = _payload(tmp_path, shear=0.0)
    fea = _fea_payload()["surface"]["fea"]
    for h in fea["hot_spots"]:
        h["faces"], h["at_m"] = ["wall"], [0.5, 0.5, 1.0]
    surf["fea"] = fea
    _open(live, "rp-fea", surf, {"metrics": {"pass": True}, "files": FILES, "withNotes": NOTES, "notes": [],
                                 "createdAt": "2026-10-29T10:00:00Z", "endedAt": "2026-10-29T10:09:40Z"})
    got = _read(live, "rp-fea")
    assert got["verdict"].startswith("Passed with notes.") and "notes" in got["verdictCls"], got
    assert got["keys"] == ["Elements", "Worst quality", "Walls OK", "Time"], got
    # Everything first, then each file, then the Mesh report
    assert got["downloads"][0] == "Everything" and got["downloads"][1] == "Nastran bulk data (.bdf)", got
    assert "Mesh report" in got["downloads"], got
    # two folded lists, closed at first
    assert [o for _s, o in got["drops"]] == [False, False], got
    assert got["drops"][0][0].startswith("Notes 1") and got["drops"][1][0].startswith("Hot spots 348"), got
    # 348 hot spots are four kinds, never 348 rows
    assert got["spotRows"] == 4 and got["spotKinds"][0] == "Sharp inside corners", got
    # a note keeps its title and sends its places to the report
    assert got["noteGroups"] == ["Past a limit"]
    assert "at a cusp in the CAD" in got["noteText"] and "(12.5" not in got["noteText"]
    assert "Each place is in the Mesh report." in got["noteText"]
    # the open pair stays in sight in red, and the faces accounted for in one line (MESH-8)
    assert got["alert"] and "Every face is accounted for." in got["faces"], got

    # a kind points at its places on the part, one at a time
    live.evaluate("""(() => { const d = document.querySelector('#viewer-rp-fea .rp-spots'); d.open = true;
      document.querySelector('#viewer-rp-fea .rp-spotkind[data-kind="fillet"]').click(); })()""")
    step = live.evaluate("""({at: document.querySelector('#viewer-rp-fea .rp-spotkind[data-kind="fillet"] .rp-at').textContent,
                             marker: window._vdbg['rp-fea:fea'].marker()})""")
    assert step == {"at": "1 of 26", "marker": True}, step
    # the marker fits what is on screen: the face shown alone, never a ball that covers it
    # (dev-areen 2026-10-30: a ball sized to the whole model filled the view)
    size = live.evaluate("window._vdbg['rp-fea:fea'].markerSize()")
    assert size and 0 < size["r"] <= 0.04 * size["shown"] + 1e-12, size

    # the Mesh report downloads from its route
    live.evaluate("document.getElementById('v-report-rp-fea').click()")
    live.wait_for("window.__reportAsks === 1", timeout=10, what="the report download")
    assert_clean(live, "the FEA result screen")


def test_a_passed_cfd_mesh_has_no_empty_lists(live, tmp_path):
    _open(live, "rp-cfd", _payload(tmp_path, shear=0.0),
          {"metrics": {"pass": True}, "files": FILES[1:], "notes": []})
    got = _read(live, "rp-cfd")
    assert got["verdict"].startswith("Passed.") and "pass" in got["verdictCls"], got
    assert got["keys"][0] == "Cells" and "Time" not in got["keys"], got
    assert got["drops"] == [] and got["downloads"] == ["Everything", "ParaView", "Mesh report"], got
    assert_clean(live, "the CFD result screen")


CFD_FILES = [
    {"id": "", "type": "repair_report", "label": "CAD inspection report (.json)", "size": 1100, "url": "#"},
    {"id": "", "type": "mesh_bundle", "label": "OpenFOAM case (.tar.gz)", "size": 182_800_000, "url": "#"},
    {"id": "", "type": "mesh", "label": "OpenFOAM case (.zip)", "size": 180_000_000, "url": "#"},
    {"id": "", "type": "mesh", "label": "Ansys Fluent mesh (.msh) - not yet tested in the real solver", "size": 300_000_000, "url": "#"},
    {"id": "", "type": "mesh", "label": "EnSight case (.case) - not yet tested in EnSight", "size": 100_000_000, "url": "#"},
]


def test_a_cfd_user_finds_their_solver_in_the_download_menu(live, tmp_path):
    # Areen 2026-11-14: "why do the downloads not include like all the stuff we have for FEA". Everything,
    # the OpenFOAM case, the solver files, ParaView, the Mesh report, then the small reports
    _open(live, "rp-cfdx", _payload(tmp_path, shear=0.0), {"metrics": {"pass": True}, "files": CFD_FILES, "notes": []})
    rows = live.evaluate("""[...document.querySelectorAll('#viewer-rp-cfdx .rp-dlmenu .v-dlbtn')].map(a =>
      [a.querySelector('b').textContent, (a.querySelector('em') || {}).textContent || ''])""")
    assert [r[0] for r in rows] == ["Everything", "OpenFOAM case (.zip)", "Ansys Fluent mesh (.msh)", "EnSight case (.case)",
                                    "ParaView", "Mesh report", "CAD inspection report (.json)"], rows
    tags = dict(rows)
    assert tags["OpenFOAM case (.zip)"] == "" and tags["Ansys Fluent mesh (.msh)"] == "untested", rows
    assert tags["EnSight case (.case)"] == "untested", rows
    assert_clean(live, "the CFD download menu")


def test_hot_spot_kinds_are_plural_in_plain_english(live):
    # dev-areen 2026-10-30: six notches showed as "Notchs"
    kinds = live.evaluate("""(async () => {
      const { hotSpotsHtml } = await import('/static/js/viewer/result_panel.js');
      const shapes = ['notch', 'notch', 'hole', 'hole', 'inside corners', 'inside corners', 'box', 'box',
                      'cavity', 'cavity', 'key', 'key', 'fillet'];
      const div = document.createElement('div');
      div.innerHTML = hotSpotsHtml(shapes.map((shape, i) => ({id: 'H' + i, shape})));
      return [...div.querySelectorAll('.rp-spotkind b')].map((b) => b.textContent).sort();
    })()""")
    assert kinds == ["Boxes", "Cavities", "Fillet", "Holes", "Inside corners", "Keys", "Notches"], kinds


def test_a_cfd_figure_past_its_limit_is_a_note_as_the_report_says(live, tmp_path):
    # dev-areen 2026-10-30 (job f5385d0f): non-orthogonality 65.0072 against a 65 limit showed in orange
    # under "Passed.", while the Mesh report said "Passed with notes"
    surf = _payload(tmp_path, shear=0.0)
    surf["quality"]["max_non_ortho"] = 65.0072
    surf["quality_fields"]["metrics"]["non_ortho"]["limit"] = 65
    _open(live, "rp-cfd-over", surf, {"metrics": {"pass": True}, "files": FILES[1:], "notes": []})
    got = _read(live, "rp-cfd-over")
    assert got["verdict"].startswith("Passed with notes.") and "notes" in got["verdictCls"], got
    assert got["noteGroups"] == ["Past a limit"] and "Worst non-orthogonality" in got["noteText"], got
    shown = live.evaluate("document.querySelector('#v-keys-rp-cfd-over').innerText")
    assert "65.01°" in shown, shown
    assert_clean(live, "the CFD result screen with a figure past its limit")


LONG_FILES = [
    {"id": "", "type": "mesh_bundle", "label": "Everything: .inp deck + solver files (.tar.gz)", "size": 24_400_000, "url": "#"},
    {"id": "", "type": "mesh", "label": "CAD inspection report (.json)", "size": 1100, "url": "#"},
    {"id": "", "type": "mesh", "label": "CalculiX / Abaqus deck (.inp) - solved in CalculiX; Abaqus not yet tested in the real solver",
     "size": 20_200_000, "url": "#"},
    {"id": "", "type": "mesh", "label": "Nastran bulk data (.bdf) - not yet tested in the real solver", "size": 30_600_000, "url": "#"},
    {"id": "", "type": "mesh", "label": "Ansys MAPDL archive (.cdb) - not yet tested in the real solver", "size": 37_500_000, "url": "#"},
]


def test_downloads_are_one_button_that_opens_a_menu_of_short_names(live, tmp_path):
    # Areen 2026-11-01: "make the downloads a Download that when clicked is a dropdown"; a caveat is
    # a small tag and a tooltip, never label text
    surf = _payload(tmp_path, shear=0.0)
    surf["fea"] = _fea_payload()["surface"]["fea"]
    _open(live, "rp-dl", surf, {"metrics": {"pass": True}, "files": LONG_FILES, "notes": [],
                                "createdAt": "2026-10-29T10:00:00Z", "endedAt": "2026-10-29T10:03:20Z"})
    read = """(() => { const v = document.getElementById('viewer-rp-dl');
      const t = v.querySelector('.rp-dltrig'), m = v.querySelector('.rp-dlmenu');
      return {trig: t.innerText.replace(/\\s+/g, ' ').trim(), expanded: t.getAttribute('aria-expanded'), open: !m.hidden,
              rows: [...m.querySelectorAll('.v-dlbtn')].map(a => [a.querySelector('b').textContent,
                (a.querySelector('em') || {}).textContent || '', a.title]),
              hint: !!v.querySelector('.tr-hint'), status: getComputedStyle(v.querySelector('.rp-head .v-row')).display,
              headTitle: v.querySelector('.rp-head').title,
              facts: (document.getElementById('v-facts-rp-dl').innerText || '').includes('Cells')}; })()"""
    live.wait_for("document.querySelector('#viewer-rp-dl .rp-head').title", timeout=30, what="the mesh to be read")
    closed = live.evaluate(read)
    assert closed["trig"].startswith("Download") and "MB" in closed["trig"], closed
    assert closed["expanded"] == "false" and not closed["open"], "the menu starts closed"
    live.evaluate("document.querySelector('#viewer-rp-dl .rp-dltrig').click()")
    opened = live.evaluate(read)
    assert opened["expanded"] == "true" and opened["open"], opened
    names = [r[0] for r in opened["rows"]]
    assert names[:5] == ["Everything", "CAD inspection report (.json)", "CalculiX / Abaqus deck (.inp)",
                         "Nastran bulk data (.bdf)", "Ansys MAPDL archive (.cdb)"], names
    assert "Mesh report" in names
    tags = {r[0]: (r[1], r[2]) for r in opened["rows"]}
    assert tags["Nastran bulk data (.bdf)"] == ("untested", "not yet tested in the real solver"), tags
    assert tags["CalculiX / Abaqus deck (.inp)"][0] == "partly tested", tags
    assert tags["CAD inspection report (.json)"][0] == "", tags
    # nothing said twice: no "every limit... is in the Mesh report" line, the long delivered-mesh
    # line is the verdict's tooltip, and the cell count is not repeated under the key numbers
    assert not opened["hint"] and opened["status"] == "none" and opened["headTitle"], opened
    assert not opened["facts"], "the delivered-mesh block repeats the cell count"
    # Escape and a click elsewhere close it
    live.evaluate("document.querySelector('#viewer-rp-dl .rp-dlmenu a').dispatchEvent("
                  "new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    assert not live.evaluate(read)["open"]
    live.evaluate("document.querySelector('#viewer-rp-dl .rp-dltrig').click(); document.body.click()")
    assert not live.evaluate(read)["open"]
    assert_clean(live, "the download menu")


@pytest.mark.parametrize("width", [360, 768, 1440])
def test_the_result_panel_never_scrolls_sideways(live, tmp_path, width):
    # dev-areen 2026-11-01: the panel scrolled right - the long file names would not shrink
    surf = _payload(tmp_path, shear=0.0)
    fea = _fea_payload()["surface"]["fea"]
    for h in fea["hot_spots"]:
        h["faces"], h["at_m"] = ["wall"], [0.5, 0.5, 1.0]
    surf["fea"] = fea
    live._send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1,
                                                       "mobile": width < 600})
    try:
        job = f"rp-w{width}"
        _open(live, job, surf, {"metrics": {"pass": True}, "files": LONG_FILES, "withNotes": NOTES, "notes": [],
                                "createdAt": "2026-10-29T10:00:00Z", "endedAt": "2026-10-29T10:03:20Z"})
        out = live.evaluate(f"""(() => {{
          const v = document.getElementById('viewer-{job}'), bar = v.querySelector('.v-bar');
          v.querySelector('.rp-dltrig').click();
          v.querySelectorAll('details.rp-drop').forEach(d => {{ d.open = true; }});
          const doc = document.scrollingElement, edge = bar.getBoundingClientRect().right + 1;
          const wide = [...bar.querySelectorAll('*')].filter(e => e.getClientRects().length
                                                             && e.getBoundingClientRect().right > edge)
                                                   .map(e => String(e.className || e.tagName));
          return {{bar: [bar.scrollWidth, bar.clientWidth], page: [doc.scrollWidth, doc.clientWidth],
                   wide: wide.slice(0, 8)}};
        }})()""")
    finally:
        live._send("Emulation.clearDeviceMetricsOverride", {})
    assert out["bar"][0] <= out["bar"][1], out
    assert out["page"][0] <= out["page"][1], out
    assert out["wide"] == [], out
