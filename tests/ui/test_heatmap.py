# Responsibility: Verify the quality heatmap colours a real mesh in a real browser and explains a face.
# Boundaries: the payload is the one the backend builds from a polyMesh; only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean
from tests.foam_fixtures import ROW_SHEAR, ROW_SHEAR_NON_ORTHO_DEG, write_row_of_hexes

pytestmark = pytest.mark.ui

JOB = "heat-job"
# the calm end of the scale, the edge colour while the faces carry it, and the plain view's edge
# colour - viewer.js's STOPS[0], _EDGE_HEAT and _EDGE
CALM = [92, 120, 152]
HEAT_EDGE = [0.24, 0.23, 0.22]
PLAIN_EDGE = [0.27, 0.25, 0.22]


def _payload(tmp_path, shear: float = ROW_SHEAR) -> dict:
    # THE BACKEND'S OWN PAYLOAD for a mesh whose numbers are known on paper: cell 2 is sheared,
    # so the face between cells 1 and 2 is 68.2 degrees non-orthogonal and everything cell 0
    # touches is orthogonal. What the browser colours is exactly what the worker would ship.
    # With shear=0 every cell is a unit cube: a mesh that passes, with no hotspot at all.
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    write_row_of_hexes(tmp_path / "constant" / "polyMesh", shear_last_y=shear)
    surf = polymesh_viewer(tmp_path, roles={"wall": "wall", "inlet": "inlet",
                                            "outlet": "outlet"}, units="m")
    assert surf and "quality_fields" in surf
    surf.update(is_mesh=True, cell_count=3, parts=[],
                quality={"cells": 3, "faces": 16, "engine": "snappy", "units": "m",
                         "max_non_ortho": round(ROW_SHEAR_NON_ORTHO_DEG, 1) if shear else 0.0,
                         "max_skewness": round(surf["quality_fields"]["metrics"]["skewness"]["max"], 3)})
    return surf


def _open(live, payload: dict, job: str) -> None:
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
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", job), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{job}:heat']", timeout=90,
                  what="the viewer to initialise with quality fields")


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
        if (s.includes('/simulation/__JOB__/surface.vtk')) {
          window.__vtkHits = (window.__vtkHits || 0) + 1;
          return Promise.resolve(new Response(new Blob(['# vtk DataFile Version 3.0\\n']),
            {status: 200, headers: {'Content-Type': 'application/octet-stream'}}));
        }
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
    assert state["metrics"] == ["non_ortho", "skewness", "aspect_ratio"]
    assert sorted(state["live"]) == ["aspect_ratio", "non_ortho", "skewness"], state
    assert state["active"] is None and state["legend"] is False

    # the viewer opens z-up, as CAD is drawn, and Fit keeps it that way
    up = live.evaluate(f"""(() => {{
      const v = window._vdbg['{JOB}'], open = v.up();
      document.getElementById('v-fitbtn-{JOB}').click();
      return {{open, fit: v.up()}};
    }})()""")
    assert up["open"] == pytest.approx([0, 0, 1], abs=1e-9), up
    assert up["fit"] == pytest.approx([0, 0, 1], abs=1e-9), up

    # clicking the non-orthogonality figure colours the mesh, draws the legend, says the one face
    # over the bar, and switches the hint to the probe
    after = live.evaluate(f"""(() => {{
      document.querySelector('#v-facts-{JOB} .mx-c.live[data-metric=non_ortho]').click();
      const h = window._vdbg['{JOB}:heat'];
      const lg = document.querySelector('#viewer-{JOB} .v-legend');
      return {{active: h.active(), legend: h.legend(), extras: h.extras(),
               legendText: lg ? lg.textContent : '',
               buttons: lg ? [...lg.querySelectorAll('button')].map(b => b.className) : [],
               hint: document.getElementById('v-hint-{JOB}').textContent,
               onCells: document.querySelectorAll('#v-facts-{JOB} .mx-c.live.on').length,
               calm: h.colorOf('inlet', 0), edges: h.edges()}};
    }})()""")
    assert after["active"] == "non_ortho" and after["legend"] is True
    assert "65.0°" in after["legendText"] and "1 face over" in after["legendText"]
    assert "Click a face" in after["hint"] and after["onCells"] == 1
    # the colour is the surface's own: nothing is drawn on top of the mesh - no markers, no walker,
    # no grey-out switch - and the scale's only control is the one that turns it off
    assert after["extras"] == 0, after
    assert after["buttons"] == ["x"], after
    # the console's original deep-steel calm end, and dark edges so every cell reads under the data
    assert after["calm"] == CALM, after
    assert after["edges"]["on"] is True, after
    assert after["edges"]["color"] == pytest.approx(HEAT_EDGE), after

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

    # aspect ratio: cell 0 is a unit cube and reads 1.00; checkMesh's bar (1000) is far above
    # the mesh's range, so the scale spans the mesh's own values and no limit tick is drawn
    ar = live.evaluate(f"""(() => {{
      const h = window._vdbg['{JOB}:heat'];
      h.set(null); h.set('aspect_ratio');
      const lg = document.querySelector('#viewer-{JOB} .v-legend');
      return {{active: h.active(), text: lg ? lg.textContent : '',
               tick: !!document.querySelector('#viewer-{JOB} .v-legend .lim'),
               inlet: h.probe('inlet', 0).values.aspect_ratio}};
    }})()""")
    assert ar["active"] == "aspect_ratio" and "aspect ratio" in ar["text"], ar
    assert ar["tick"] is False and "none near the 1000.00 bar" in ar["text"], ar
    assert ar["inlet"] == pytest.approx(1.0, abs=1e-4)

    # the ParaView export button fetches the VTK route with the API headers and hands the file
    # to the browser as a download
    exported = live.evaluate(f"""(async () => {{
      const a = window._vdbg['{JOB}'].vtk && window._vdbg['{JOB}'].vtk();
      if (!a) return {{present: false}};
      a.click();
      await new Promise(r => setTimeout(r, 800));
      return {{present: true, hits: window.__vtkHits || 0, id: a.id, label: a.textContent}};
    }})()""", timeout=30)
    assert exported["present"] and exported["hits"] == 1 and exported["id"] == f"v-vtk-{JOB}", exported

    # turning it off restores the plain view: no legend, no active metric, nothing drawn over the
    # mesh, the property colours back and the edges drawn as the parts panel draws them
    off = live.evaluate(f"""(() => {{
      window._vdbg['{JOB}:heat'].set(null);
      const h = window._vdbg['{JOB}:heat'];
      return {{active: h.active(), legend: h.legend(), colour: h.colorOf('inlet', 0), edges: h.edges(),
               hint: document.getElementById('v-hint-{JOB}').textContent}};
    }})()""")
    assert off["active"] is None and off["legend"] is False and off["colour"] is None, off
    assert off["edges"]["on"] is True and off["edges"]["color"] == pytest.approx(PLAIN_EDGE), off
    assert "Rotate: drag" in off["hint"]
    assert_clean(live, "the viewer with the heatmap")


def test_a_mesh_that_passes_reads_calm_with_nothing_drawn_on_it(live, tmp_path):
    # A good mesh has no hotspot at all - the payload's list is the faces OVER the bar. Its heatmap
    # is the calm colour on every face, the dark cell edges over it, and nothing else.
    payload = _payload(tmp_path, shear=0.0)
    assert not [h for h in payload["quality_fields"]["hotspots"] if h["metric"] == "non_ortho"]
    _open(live, payload, "good-job")
    state = live.evaluate("""(() => {
      document.querySelector('#v-facts-good-job .mx-c.live[data-metric=non_ortho]').click();
      const h = window._vdbg['good-job:heat'];
      const lg = document.querySelector('#viewer-good-job .v-legend');
      return {text: lg.textContent, extras: h.extras(), calm: h.colorOf('inlet', 0),
              outlet: h.colorOf('outlet', 0), edges: h.edges(),
              buttons: [...lg.querySelectorAll('button')].map(b => b.className)};
    })()""")
    assert "none over" in state["text"] and state["buttons"] == ["x"], state
    assert state["extras"] == 0, state
    assert state["calm"] == CALM and state["outlet"] == CALM, state
    assert state["edges"]["on"] is True and state["edges"]["color"] == pytest.approx(HEAT_EDGE), state
    assert_clean(live, "the heatmap on a mesh that passes")


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
      legend: !!document.querySelector('#viewer-plain-job .v-legend'),
      vtk: !!document.getElementById('v-vtk-plain-job')}))()""")
    # the export needs no fields, only a polyMesh surface - it stays
    assert state == {"heat": False, "live": 0, "ctl": False, "legend": False, "vtk": True}
    assert_clean(live, "the viewer without quality fields")
