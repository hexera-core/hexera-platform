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
# the flat grey "Only bad cells" rests every face below 80% of the limit in - viewer.js's NEUTRAL
NEUTRAL = [84, 87, 92]


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
               calm: h.colorOf('inlet', 0), edges: h.edges(), areas: h.areas()}};
    }})()""")
    assert after["active"] == "non_ortho" and after["legend"] is True
    assert "65.0°" in after["legendText"] and "1 face over" in after["legendText"]
    assert "Click a face" in after["hint"] and after["onCells"] == 1
    # the colour is the surface's own: nothing is drawn on top of the mesh. The scale's controls are
    # the one that turns it off, "Only bad cells", and the one problem area the sheared cells make
    assert after["extras"] == 0, after
    assert after["buttons"] == ["x", "v-only", "v-area"], after
    assert len(after["areas"]) == 1, after
    area = after["areas"][0]
    assert area["worst"] == pytest.approx(ROW_SHEAR_NON_ORTHO_DEG, abs=0.1), area
    assert area["over"] > 0 and area["near"] == 0 and area["index"] == 0, area
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
      const box = lg.querySelector('.v-areas');
      return {text: lg.textContent, extras: h.extras(), calm: h.colorOf('inlet', 0),
              outlet: h.colorOf('outlet', 0), edges: h.edges(), areas: h.areas(),
              empty: box ? box.textContent : null,
              buttons: [...lg.querySelectorAll('button')].map(b => b.className)};
    })()""")
    assert "none over" in state["text"] and state["buttons"] == ["x"], state
    # no problem areas: one quiet line, no list and no "Only bad cells" switch
    assert state["areas"] == [], state
    assert state["empty"].strip() == "No problem areas: every cell is well inside the limit", state
    assert state["extras"] == 0, state
    assert state["calm"] == CALM and state["outlet"] == CALM, state
    assert state["edges"]["on"] is True and state["edges"]["color"] == pytest.approx(HEAT_EDGE), state
    assert_clean(live, "the heatmap on a mesh that passes")


PLATE_NX, PLATE_NY = 60, 20


def _plate_payload() -> dict:
    """A flat plate of unit squares with two bad places on it, far apart: two faces over the 65
    degree bar inside a ring of ten faces near it (56 degrees, above 80% of the bar), and nine
    faces near it only (60 degrees). Every other face reads 10 degrees."""
    import base64

    import numpy as np
    nx, ny = PLATE_NX, PLATE_NY
    xs, ys = np.meshgrid(np.arange(nx + 1, dtype=np.float32), np.arange(ny + 1, dtype=np.float32))
    pts = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size, dtype=np.float32)], axis=1)
    polys: list[int] = []
    vals: list[float] = []
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            polys += [4, a, a + 1, a + nx + 2, a + nx + 1]
            v = 10.0
            if 9 <= i <= 12 and 9 <= j <= 11:
                v = 56.0
            if i in (10, 11) and j == 10:
                v = 70.0
            if 44 <= i <= 46 and 9 <= j <= 11:
                v = 60.0
            vals.append(v)

    def b64(a) -> str:
        return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")
    n = nx * ny
    return {"kind": "polymesh", "is_mesh": True, "mesh_units": "m", "cell_count": n, "parts": [],
            "patches": [{"name": "plate", "type": "wall", "face_count": n, "points_b64": b64(pts),
                         "polys_b64": b64(np.array(polys, dtype=np.uint32))}],
            "quality": {"cells": n, "max_non_ortho": 70.0},
            "quality_fields": {
                "basis": "owner_cell_max",
                "metrics": {"non_ortho": {"label": "non-orthogonality", "unit": "°", "limit": 65.0,
                                          "max": 70.0, "n_over": 2, "n_faces": n}},
                "patches": {"plate": {"non_ortho_b64": b64(np.array(vals, dtype=np.float32)), "count": n}},
                "hotspots": []}}


def _face(i: int, j: int) -> int:
    return j * PLATE_NX + i


def test_problem_areas_gather_the_bad_faces_and_take_the_camera_there(live):
    _open(live, _plate_payload(), "plate-job")
    # the faces over and near the bar are gathered into places, worst first, under the scale
    st = live.evaluate("""(() => {
      const h = window._vdbg['plate-job:heat']; h.set('non_ortho');
      const box = document.querySelector('#viewer-plate-job .v-legend .v-areas');
      return {areas: h.areas(), total: h.totalAreas(), extras: h.extras(),
              text: box ? box.textContent : '',
              items: [...box.querySelectorAll('button.v-area')]
                .map(b => [b.dataset.index, b.getAttribute('aria-pressed')]),
              only: box.querySelector('button.v-only').getAttribute('aria-pressed')};
    })()""")
    areas = st["areas"]
    assert [(a["over"], a["near"]) for a in areas] == [(2, 10), (0, 9)], st
    assert areas[0]["worst"] == pytest.approx(70.0) and areas[1]["worst"] == pytest.approx(60.0), st
    assert {a["patch"] for a in areas} == {"plate"} and all(a["hint"] for a in areas), st
    assert areas[0]["centre"] == pytest.approx([11.0, 10.5, 0.0], abs=0.75), st
    assert areas[1]["centre"] == pytest.approx([45.5, 10.5, 0.0], abs=0.75), st
    assert st["total"] == 2 and st["extras"] == 0, st
    assert "Problem areas" in st["text"] and "2 over the limit · 10 near" in st["text"], st
    assert "9 near the limit" in st["text"] and "70.0°" in st["text"], st
    assert st["items"] == [["0", "false"], ["1", "false"]] and st["only"] == "false", st

    # a click on the second area glides the camera there - centred on it, close enough that its
    # cells read as cells - and marks it as the one chosen; nothing is drawn on the mesh
    moving = live.evaluate("""(() => {
      document.querySelector('#viewer-plate-job button.v-area[data-index="1"]').click();
      return window._vdbg['plate-job:heat'].flying();
    })()""")
    assert moving is True
    live.wait_for("!window._vdbg['plate-job:heat'].flying()", timeout=10, what="the glide to land")
    moved = live.evaluate("""(() => { const h = window._vdbg['plate-job:heat'];
      return {focal: h.focal(), px: h.cellPx(1), sel: h.selected(), extras: h.extras(),
              pressed: [...document.querySelectorAll('#viewer-plate-job button.v-area')]
                .map(b => b.getAttribute('aria-pressed'))}; })()""")
    assert moved["focal"] == pytest.approx(areas[1]["centre"], abs=1e-6), moved
    assert 12 <= moved["px"] <= 20.5, moved
    assert moved["sel"] == 1 and moved["pressed"] == ["false", "true"] and moved["extras"] == 0, moved
    # the API the demo recorder drives: goArea resolves when the camera has landed
    landed = live.evaluate("window._vdbg['plate-job:heat'].goArea(0)", timeout=15)
    back = live.evaluate("window._vdbg['plate-job:heat'].focal()")
    assert landed is True and back == pytest.approx(areas[0]["centre"], abs=1e-6), back

    # "Only bad cells" greys every face below 80% of the bar and leaves the bad ones coloured;
    # pressed again, the colours come back
    grey = live.evaluate(f"""(() => {{
      const h = window._vdbg['plate-job:heat'], btn = () => document.querySelector('#viewer-plate-job button.v-only');
      const good = {_face(30, 2)}, near = {_face(9, 10)}, over = {_face(10, 10)};
      const before = {{good: h.colorOf('plate', good), near: h.colorOf('plate', near), over: h.colorOf('plate', over)}};
      btn().click();
      const on = {{good: h.colorOf('plate', good), near: h.colorOf('plate', near), over: h.colorOf('plate', over),
                  pressed: btn().getAttribute('aria-pressed'), state: h.onlyBad()}};
      btn().click();
      return {{before, on, back: h.colorOf('plate', good), state: h.onlyBad(),
               pressed: btn().getAttribute('aria-pressed')}};
    }})()""")
    assert grey["on"]["good"] == NEUTRAL and grey["on"]["pressed"] == "true" and grey["on"]["state"] is True, grey
    assert grey["on"]["near"] == grey["before"]["near"] != NEUTRAL, grey
    assert grey["on"]["over"] == grey["before"]["over"] != NEUTRAL, grey
    assert grey["back"] == grey["before"]["good"] != NEUTRAL, grey
    assert grey["state"] is False and grey["pressed"] == "false", grey
    assert_clean(live, "the problem areas")


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
