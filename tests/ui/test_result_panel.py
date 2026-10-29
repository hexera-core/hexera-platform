# Responsibility: Verify the short result screen (revamp 5/5) in a real browser - one verdict line, at most four key numbers, the downloads with Everything first and the Mesh report last, Notes and Hot spots each one folded list grouped by kind (348 hot spots are four rows, never 348), a kind that points at its places on the part, and a Mesh report that downloads from its route.
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
    assert got["drops"] == [] and got["downloads"] == ["Everything", "Mesh report", "ParaView"], got
    assert_clean(live, "the CFD result screen")
