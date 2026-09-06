# Responsibility: Verify the quality heatmap colours a real mesh in a real browser and explains a face.
# Boundaries: the payload is the one the backend builds from a polyMesh; only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

from tests.foam_fixtures import ROW_SHEAR, ROW_SHEAR_NON_ORTHO_DEG, write_row_of_hexes

pytestmark = pytest.mark.ui

JOB = "heat-job"


def _payload(tmp_path) -> dict:
    # THE BACKEND'S OWN PAYLOAD for a mesh whose numbers are known on paper: cell 2 is sheared,
    # so the face between cells 1 and 2 is 68.2 degrees non-orthogonal and everything cell 0
    # touches is orthogonal. What the browser colours is exactly what the worker would ship.
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    write_row_of_hexes(tmp_path / "constant" / "polyMesh", shear_last_y=ROW_SHEAR)
    surf = polymesh_viewer(tmp_path, roles={"wall": "wall", "inlet": "inlet",
                                            "outlet": "outlet"}, units="m")
    assert surf and "quality_fields" in surf
    surf.update(is_mesh=True, cell_count=3, parts=[],
                quality={"cells": 3, "faces": 16, "engine": "snappy", "units": "m",
                         "max_non_ortho": round(ROW_SHEAR_NON_ORTHO_DEG, 1),
                         "max_skewness": round(surf["quality_fields"]["metrics"]["skewness"]["max"], 3)})
    return surf


def test_the_heatmap_colours_the_mesh_and_explains_a_face(live, tmp_path):
    payload = _payload(tmp_path)
    # Only the network is stood in for: the surface and the client config come from here, every
    # other request still leaves the browser. The viewer module itself is the shipped one.
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
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
      document.getElementById('viewer-__JOB__').scrollIntoView();
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}:heat']", timeout=90,
                  what="the viewer to initialise with quality fields")

    # the delivered-mesh FIGURES became the controls, one per metric the payload carries
    state = live.evaluate(f"""(() => {{
      const h = window._vdbg['{JOB}:heat'];
      const live = [...document.querySelectorAll('#v-facts-{JOB} .mx-c.live')]
        .map(c => c.dataset.metric);
      return {{metrics: h.metrics, live, active: h.active(), legend: h.legend()}};
    }})()""")
    assert state["metrics"] == ["non_ortho", "skewness"]
    assert sorted(state["live"]) == ["non_ortho", "skewness"], state
    assert state["active"] is None and state["legend"] is False

    # clicking the non-orthogonality figure colours the mesh, draws the legend, marks the one
    # face over the bar, and switches the hint to the probe
    after = live.evaluate(f"""(() => {{
      document.querySelector('#v-facts-{JOB} .mx-c.live[data-metric=non_ortho]').click();
      const h = window._vdbg['{JOB}:heat'];
      const lg = document.querySelector('#viewer-{JOB} .v-legend');
      return {{active: h.active(), legend: h.legend(), hotspots: h.hotspots(),
               legendText: lg ? lg.textContent : '',
               hint: document.getElementById('v-hint-{JOB}').textContent,
               onCells: document.querySelectorAll('#v-facts-{JOB} .mx-c.live.on').length}};
    }})()""")
    assert after["active"] == "non_ortho" and after["legend"] is True
    assert after["hotspots"] == 1, "exactly one face is over the bar"
    assert "65.0°" in after["legendText"] and "1 face over" in after["legendText"]
    assert "click a face" in after["hint"] and after["onCells"] == 1

    # a probe on the outlet (cell 2) reads the sheared angle and says it is over the bar; the
    # inlet (cell 0) reads zero. The reason names the cell's shape.
    probe = live.evaluate(f"""(() => {{
      const h = window._vdbg['{JOB}:heat'];
      const out = h.probe('outlet', 0);
      const text = document.querySelector('#viewer-{JOB} .v-probe').textContent;
      const inl = h.probe('inlet', 0);
      return {{out, text, inl}};
    }})()""")
    assert probe["out"]["values"]["non_ortho"] == pytest.approx(ROW_SHEAR_NON_ORTHO_DEG, abs=0.1)
    assert probe["out"]["over"] == ["non_ortho"] and probe["out"]["cellFaces"] == 6
    assert "over the 65.0° limit" in probe["text"] and "6-face hex" in probe["text"]
    assert "likely:" in probe["text"]
    assert probe["inl"]["values"]["non_ortho"] == pytest.approx(0.0, abs=1e-5)
    assert probe["inl"]["over"] == []

    # "Mark this spot" hands the face to the existing dispute path: one marker, ready to send
    marked = live.evaluate(f"""(() => {{
      window._vdbg['{JOB}:heat'].probe('outlet', 0);
      document.querySelector('#viewer-{JOB} .v-probe [data-a=mark]').click();
      return {{markers: window._vdbg['{JOB}'].markers(),
               probeGone: !document.querySelector('#viewer-{JOB} .v-probe'),
               submit: document.getElementById('v-submit-{JOB}').textContent}};
    }})()""")
    assert marked["markers"] == 1 and marked["probeGone"]
    assert "1 mark" in marked["submit"]

    # turning it off restores the plain view: no legend, no hotspots, no active metric
    off = live.evaluate(f"""(() => {{
      window._vdbg['{JOB}:heat'].set(null);
      const h = window._vdbg['{JOB}:heat'];
      return {{active: h.active(), legend: h.legend(), hotspots: h.hotspots(),
               hint: document.getElementById('v-hint-{JOB}').textContent}};
    }})()""")
    assert off["active"] is None and off["legend"] is False and off["hotspots"] == 0
    assert "rotate: drag" in off["hint"]
    assert_clean(live, "the viewer with the heatmap")


def test_a_payload_without_fields_leaves_the_viewer_exactly_as_before(live, tmp_path):
    payload = _payload(tmp_path)
    payload.pop("quality_fields")
    live.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/plain-job/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('plain-job', document.getElementById('stage'), {});
      document.getElementById('viewer-plain-job').scrollIntoView();
    })()""".replace("__PAYLOAD__", json.dumps(payload)), timeout=60)
    live.wait_for("window._vdbg && window._vdbg['plain-job']", timeout=90,
                  what="the viewer to initialise")
    state = live.evaluate("""(() => ({
      heat: !!(window._vdbg['plain-job:heat']),
      live: document.querySelectorAll('#v-facts-plain-job .mx-c.live').length,
      ctl: !!document.getElementById('v-heatctl-plain-job'),
      legend: !!document.querySelector('#viewer-plain-job .v-legend')}))()""")
    assert state == {"heat": False, "live": 0, "ctl": False, "legend": False}
    assert_clean(live, "the viewer without quality fields")
