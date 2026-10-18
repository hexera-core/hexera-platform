# Responsibility: Verify a delivered thermal model opens in a real browser as an electronics-cooling
# engineer reads it: every part in a table by the file's name, the quality in words, the per-part
# table and the thermal model as downloads, a slice that stands in for the surfaces, a part shown
# alone from its row - and no generic CFD word anywhere on the panel.
# Boundaries: the payload is the one the worker builds from a real snap-grid case of a test model
# (engines/snapgrid/viewer + results, application/viewer_payload's facts); only the network is
# stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

JOB = "thermal-job"


def _payload(tmp_path) -> dict:
    from tests.unit.cad.ecxml_models import set_top_box

    from meshpipeline.application.viewer_payload import _attach_facts
    from meshpipeline.engines.snapgrid import grid as G
    from meshpipeline.engines.snapgrid.mesher import mesh_ecxml
    from meshpipeline.engines.snapgrid.viewer import snapgrid_viewer

    src = tmp_path / "model.ecxml"
    src.write_bytes(set_top_box().xml())
    case = tmp_path / "case"
    result = mesh_ecxml(src, case, G.GridPlan(), binary=False)
    report = json.loads((case / "snapgrid_report.json").read_text())
    cells = sum(int(r["cells"]) for r in report["regions"])
    report["native"] = {"ok": True, "problems": [], "whole": {
        "mesh_ok": True, "cells": cells, "hexahedra": cells, "max_non_ortho": 40.0, "fatal": [],
        "skew_faces": 0}, "regions": {r["name"]: {"mesh_ok": True} for r in report["regions"]}}
    (case / "snapgrid_report.json").write_text(json.dumps(report))
    surf = snapgrid_viewer(case, roles={}, units="m")
    surf.update(is_mesh=True, cell_count=cells)
    regions = [{"name": n, "type": t, "cells": 1}
               for t, key in (("fluid", "fluid_regions"), ("solid", "solid_regions"))
               for n in result.report[key]]
    _attach_facts(surf, {"mesh_mode": "snapgrid", "mesh_units": "m", "patch_types": {},
                         "quality": {"regions": regions, "cells": cells}})
    assert surf["thermal"]["slices"], "the test model has slices to show"
    return surf


def _open(live, payload: dict) -> None:
    live.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__JOB__/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true},
        files: [{type: 'mesh_bundle', label: 'OpenFOAM case (multi-region) (.tar.gz)', size: 1000, url: '#'}]});
      document.getElementById('viewer-__JOB__').scrollIntoView();
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}:thermal']", timeout=90,
                  what="the viewer to show the thermal model's results")


def test_a_delivered_thermal_model_reads_part_by_part(live, tmp_path):
    from meshpipeline.application.thermal_wording import forbidden_words

    payload = _payload(tmp_path)
    t = payload["thermal"]
    _open(live, payload)
    D = f"window._vdbg['{JOB}:thermal']"
    seen = live.evaluate(f"""(() => {{
      const rows = [...document.querySelectorAll('#viewer-{JOB} .tr-row')].map(r => r.dataset.part);
      return {{rows, status: document.getElementById('v-status-{JOB}').textContent,
               panel: document.querySelector('#viewer-{JOB} .v-bar').innerText,
               csv: !!document.getElementById('v-csv-{JOB}'), model: !!document.getElementById('v-tm-{JOB}'),
               groups: [...document.querySelectorAll('#viewer-{JOB} .v-bnd-gl')].map(g => g.textContent)}};
    }})()""")
    assert seen["rows"] == [p["name"] for p in t["parts"]]
    assert seen["status"].startswith("your delivered thermal mesh") and t["summary"] in seen["status"]
    assert seen["csv"] and seen["model"]
    assert "Parts" in seen["groups"] and "Slices" not in seen["groups"]
    for line in t["quality"]:
        assert line in seen["panel"]
    names = [p["name"] for p in payload["patches"]] + [p["label"] for p in t["parts"]]
    assert forbidden_words(seen["panel"], names) == [], seen["panel"]

    # a slice stands in for the surfaces; the whole model comes back after it
    first = t["slices"][0]["name"]
    live.evaluate(f"{D}.slice({json.dumps(first)})")
    assert live.evaluate(f"{D}.sliceOn()") == first
    assert live.evaluate(f"{D}.visible()") == []
    assert first in live.evaluate(f"{D}.slices()")
    live.evaluate(f"{D}.slice(null)")
    assert "PCB" in live.evaluate(f"{D}.visible()")

    # a row shows its part alone, and again shows the whole model
    live.evaluate(f"document.querySelector('#viewer-{JOB} .tr-row[data-part=\"PCB\"]').click()")
    assert live.evaluate(f"{D}.visible()") == ["PCB"]
    live.evaluate(f"document.querySelector('#viewer-{JOB} .tr-row[data-part=\"PCB\"]').click()")
    assert len(live.evaluate(f"{D}.visible()")) > 1
    assert_clean(live, "the thermal results")


def test_the_table_sorts_and_finds(live, tmp_path):
    payload = _payload(tmp_path)
    _open(live, payload)
    order = live.evaluate(f"""(() => {{
      const th = [...document.querySelectorAll('#viewer-{JOB} .tr-tab th')].find(h => h.dataset.k === 'cells');
      th.click();
      return [...document.querySelectorAll('#viewer-{JOB} .tr-row')].map(r => r.dataset.part);
    }})()""")
    cells = {p["name"]: p["cells"] for p in payload["thermal"]["parts"]}
    assert [cells[n] for n in order] == sorted(cells.values(), reverse=True)
