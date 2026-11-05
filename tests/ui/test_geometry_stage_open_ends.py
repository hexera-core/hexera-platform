# Responsibility: Verify "Add an opening" lands on the hole the user clicks near - the check's own
# holes (cad/open_ends, the definition the upload's stickers share): a click into the hole of a
# thin-walled tube snaps to its open rim, a click into a thick-walled tube's hole - or on the end
# face around it, or on the wall just beside it - snaps to the inner loop, and the snapped centre,
# size and direction reach the confirmation. Where no hole is found the sticker is never dropped
# on the wall: the stage asks for two more points around the hole's edge and places the circle
# through the three; Esc cancels. A fluid volume's mouths are faces, so its holes are not snapped
# to. A STEP file's skin snaps the same way, to the CAD face's exact numbers.
# Boundaries: the skin and the holes are built through the worker's own code from geometry made
# here; only the network is stood in for. The rays are given in the part's coordinates (one test
# drives a real click on the canvas, through the camera).
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

ORIGIN = (0.1, 0.2, 0.3)          # the tube's first end, in metres; it runs 0.3 m along +x
LENGTH, R_OUT, R_IN, SIDES = 0.3, 0.05, 0.04, 48


def _equivalent_mm(r: float, sides: int = SIDES) -> float:
    """The diameter of the circle with the area of the regular polygon the tube's loop is."""
    area = 0.5 * sides * r * r * math.sin(2 * math.pi / sides)
    return 2000.0 * math.sqrt(area / math.pi)


def _tube(r_out: float, r_in: float | None) -> np.ndarray:
    """A straight tube along +x from ORIGIN, as triangles. With `r_in` it is a thick wall - outer
    wall, inner wall and a flat ring at each end; without, a thin wall open at both ends."""
    ox, oy, oz = ORIGIN

    def at(x, r, k):
        a = 2 * math.pi * k / SIDES
        return (ox + x, oy + r * math.cos(a), oz + r * math.sin(a))

    tris = []
    for k in range(SIDES):
        a0, a1 = at(0, r_out, k), at(0, r_out, k + 1)
        b0, b1 = at(LENGTH, r_out, k), at(LENGTH, r_out, k + 1)
        tris += [(a0, b1, b0), (a0, a1, b1)]                       # outer wall, facing out
        if r_in is None:
            continue
        c0, c1 = at(0, r_in, k), at(0, r_in, k + 1)
        d0, d1 = at(LENGTH, r_in, k), at(LENGTH, r_in, k + 1)
        tris += [(c0, d0, d1), (c0, d1, c1)]                       # inner wall, facing the bore
        tris += [(a0, c0, c1), (a0, c1, a1)]                       # the ring at x = 0
        tris += [(b0, d1, d0), (b0, b1, d1)]                       # the ring at x = LENGTH
    return np.asarray(tris, dtype=float)


def _skin(tris: np.ndarray) -> dict:
    from meshpipeline.render.skin_mesh import edges_block, prepare_skin, skin_patch
    prep = prepare_skin(tris)
    return {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
            "patches": [skin_patch(prep)], "edges": edges_block(prep)}


def _holes(tris: np.ndarray) -> list:
    """The holes the check serves for these triangles: the worker's own finder, as stored."""
    from meshpipeline.cad.open_ends import find_holes
    return json.loads(json.dumps([h.as_dict() for h in find_holes(tris)]))


def _check(faces: list | None = None, holes: list | None = None, kind: str = "body-surface") -> dict:
    # the measuring step proposed no opening, so the user adds them
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "tube", "input_kind": kind, "flow": "internal",
        "size_mm": [300.0, 100.0, 100.0], "seed_point_mm": [250.0, 200.0, 300.0], "notes": [],
        "openings": [], "faces": faces or [], "holes": holes or []}}


def _open(live, sid: str, skin: dict, check: dict) -> None:
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__confirmed = window.__confirmed || {};
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stages = window.__stages || {};
      window.__stages['__S__'] = await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed['__S__'] = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(skin)).replace("__CHECK__", json.dumps(check)).replace("__S__", sid),
        timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{sid}']", timeout=90, what="the geometry stage to initialise")


def _click(live, sid: str, origin, toward) -> dict:
    """Click along the ray from `origin` toward a point, as a click there does (the add button
    armed); the opening it added, if any, its row, and where the stage stands after."""
    return live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{sid}'], root = document.getElementById('gstage-{sid}');
      if (!h.adding()) root.querySelector('.gc-add').click();
      const o = {json.dumps(list(origin))}, t = {json.dumps(list(toward))};
      const a = h.addAt({{origin: o, dir: [t[0] - o[0], t[1] - o[1], t[2] - o[2]]}});
      const tr = a && root.querySelector('tr[data-id="' + a.id + '"]');
      return {{added: !!a, id: a && a.id, kind: a && a.kind, shape: a && a.shape, end: !!(a && a.open_end), traced: !!(a && a.traced),
               d: a && a.diameter_mm, at: a && a.centroid_mm, n: a && a.normal,
               size: tr ? tr.querySelector('.gc-dim').textContent.trim() : null,
               box: tr ? !!tr.querySelector('.gc-dia') : null, sure: tr ? tr.querySelector('.gc-conf').textContent.trim() : null,
               hint: root.querySelector('.gc-tools-hint').textContent, tracing: h.tracing(), adding: h.adding(),
               rows: root.querySelectorAll('.gc-table tbody tr').length}};
    }})()""")


def _near(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_a_click_into_a_thin_walled_tubes_open_end_snaps_to_the_rim(live):
    sid = "open-thin"
    tris = _tube(R_OUT, None)
    _open(live, sid, _skin(tris), _check(holes=_holes(tris)))
    ox, oy, oz = ORIGIN
    # from outside, slanting into the hole at x = 0: the ray passes through the hole, which has no
    # surface, and meets the wall inside - the spot the old picking put the sticker on
    o = _click(live, sid, (ox - 0.2, oy, oz + 0.1), (ox + 0.03, oy + 0.01, oz))
    assert o["end"] is True and o["kind"] == "rim" and o["shape"] == "circle", o
    assert _near(o["at"], [ox * 1000, oy * 1000, oz * 1000], 0.05), o
    assert abs(o["d"] - _equivalent_mm(R_OUT)) <= 0.05, o
    assert _near(o["n"], [-1.0, 0.0, 0.0], 1e-4), o                       # out of the part
    assert o["size"].startswith("99.9") and o["box"] is False and o["sure"] == "you", o
    assert "hole" in o["hint"] and o["adding"] is False, o
    far = _click(live, sid, (ox + LENGTH + 0.2, oy, oz - 0.1), (ox + LENGTH - 0.03, oy, oz + 0.01))
    assert far["end"] is True and _near(far["at"], [(ox + LENGTH) * 1000, oy * 1000, oz * 1000], 0.05), far
    assert _near(far["n"], [1.0, 0.0, 0.0], 1e-4), far
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "the stage snapping to a thin wall's open end")


def test_a_thick_walled_tube_snaps_to_the_inner_loop_and_proceeds_with_it(live):
    sid = "open-thick"
    tris = _tube(R_OUT, R_IN)
    _open(live, sid, _skin(tris), _check(holes=_holes(tris)))
    ox, oy, oz = ORIGIN
    inner_mm = _equivalent_mm(R_IN)
    end0 = [ox * 1000, oy * 1000, oz * 1000]
    # INTO THE HOLE: the ray meets the bore's wall behind it, and the opening is the inner loop
    into = _click(live, sid, (ox - 0.2, oy, oz + 0.1), (ox + 0.03, oy + 0.01, oz))
    assert into["end"] is True and into["kind"] == "bore", into
    assert abs(into["d"] - inner_mm) <= 0.05 and into["d"] < 81.0, into        # the hole, not the wall's outside
    assert _near(into["at"], end0, 0.05) and _near(into["n"], [-1.0, 0.0, 0.0], 1e-4), into
    # ON THE END FACE, between the two loops: the same inner loop
    rim = _click(live, sid, (ox - 0.2, oy + 0.045, oz + 0.05), (ox, oy + 0.045, oz))
    assert rim["end"] is True and abs(rim["d"] - inner_mm) <= 0.05 and _near(rim["at"], end0, 0.05), rim
    # ON THE OUTER WALL 5 mm from the end, from above: no plane is looked through, but the end is
    # right there - the nearest hole
    beside = _click(live, sid, (ox + 0.005, oy, oz + 0.4), (ox + 0.005, oy, oz + R_OUT))
    assert beside["end"] is True and abs(beside["d"] - inner_mm) <= 0.05 and _near(beside["at"], end0, 0.05), beside
    # nothing at all along the ray: nothing happens, and the button stays armed
    nothing = _click(live, sid, (ox + 0.15, oy + 0.3, oz + 0.4), (ox + 0.15, oy + 0.3, oz))
    assert nothing["added"] is False and nothing["tracing"] is None and nothing["adding"] is True, nothing
    live.evaluate(f"document.getElementById('gstage-{sid}').querySelector('.gc-add').click()")

    # PROCEED: the snapped opening reaches the confirmation with its centre, size and direction
    live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('tr[data-id="{rim["id"]}"] .gc-x').click();
      root.querySelector('tr[data-id="{beside["id"]}"] .gc-x').click();
      root.querySelector('tr[data-id="{into["id"]}"] .gc-role').value = 'inlet';
      root.querySelector('.gc-proceed').click();
    }})()""")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=30, what="the confirmation to be sent")
    body = live.evaluate(f"window.__confirmed['{sid}']")
    (sent,) = body["openings"]
    assert sent["role"] == "inlet" and abs(sent["diameter_mm"] - inner_mm) <= 0.05, sent
    assert _near(sent["centroid_mm"], end0, 0.05) and _near(sent["normal"], [-1.0, 0.0, 0.0], 1e-4), sent
    assert_clean(live, "the stage snapping to a thick wall's end")


def test_with_no_hole_there_the_stage_asks_for_its_edge_and_places_the_circle(live):
    # the check found no hole (an old check, or a hole the finder missed): the first click starts
    # a trace instead of dropping a sticker on the wall; two more points on the edge make the circle
    sid = "open-trace"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check(holes=[]))
    ox, oy, oz = ORIGIN

    def on_face(deg):                    # a point on the end face, 1 mm out from the bore's edge
        a = math.radians(deg)
        p = (ox, oy + 0.041 * math.cos(a), oz + 0.041 * math.sin(a))
        return (p[0] - 0.2, p[1] * 1.0, p[2] + 0.02), p

    first = _click(live, sid, *on_face(10))
    assert first["added"] is False and first["tracing"] == 1 and first["rows"] == 0, first
    assert "Couldn't find a hole here: click 2 more points around its edge" in first["hint"], first
    second = _click(live, sid, *on_face(130))
    assert second["added"] is False and second["tracing"] == 2 and "1 more point" in second["hint"], second
    third = _click(live, sid, *on_face(250))
    assert third["added"] is True and third["traced"] is True and third["shape"] == "circle", third
    assert abs(third["d"] - 82.0) < 0.2 and _near(third["at"], [ox * 1000, oy * 1000, oz * 1000], 0.2), third
    assert _near(third["n"], [-1.0, 0.0, 0.0], 1e-3), third             # toward the camera: out of the part
    assert third["tracing"] is None and third["adding"] is False and "circle" in third["hint"], third
    assert third["box"] is False and third["sure"] == "you", third

    # ESC CANCELS a trace half way: no sticker, the button disarmed, the dots gone
    _click(live, sid, *on_face(40))
    live.press("Escape", vk=27)
    after = live.evaluate(f"""(() => {{ const h = window._vdbg['gstage:{sid}'];
      return {{tracing: h.tracing(), adding: h.adding(), rows: document.querySelectorAll('#gstage-{sid} .gc-table tbody tr').length}}; }})()""")
    assert after == {"tracing": None, "adding": False, "rows": 1}, after
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "tracing a hole the check did not find")


def test_a_fluid_volumes_mouths_are_faces_not_holes(live):
    # the user says the file is the fluid volume itself: a hole in it is not a mouth (the space
    # a hub stands in), so a click into it is not snapped to it - the stage asks for the edge
    sid = "open-fluid"
    tris = _tube(R_OUT, R_IN)
    _open(live, sid, _skin(tris), _check(holes=_holes(tris), kind="fluid-domain"))
    ox, oy, oz = ORIGIN
    o = _click(live, sid, (ox - 0.2, oy, oz + 0.1), (ox + 0.03, oy + 0.01, oz))
    assert o["added"] is False and o["tracing"] == 1, o
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "the stage on a fluid volume")


def test_a_real_click_on_the_canvas_into_the_hole_snaps_through_the_camera(live):
    sid = "open-click"
    tris = _tube(R_OUT, R_IN)
    _open(live, sid, _skin(tris), _check(holes=_holes(tris)))
    ox, oy, oz = ORIGIN
    # a click with nothing armed adds nothing - and marks the camera the user's, so the stage no
    # longer re-frames the part on its own
    centre = live.evaluate(f"""(() => {{ const r = document.getElementById('gs-canvas-{sid}').getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    live.click(*centre)
    assert live.evaluate(f"window._vdbg['gstage:{sid}'].openings().length") == 0
    # the camera looks into the x = 0 end from above and outside, at a point 50 mm down the bore:
    # the canvas's centre pixel sees through the hole onto the bore's wall
    aim = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{sid}'], root = document.getElementById('gstage-{sid}');
      const host = document.getElementById('gs-canvas-{sid}');
      const cam = h.cam();
      cam.setFocalPoint({ox + 0.05}, {oy}, {oz}); cam.setPosition({ox - 0.25}, {oy}, {oz + 0.12}); cam.setViewUp(0, 0, 1);
      h.render();
      root.querySelector('.gc-add').click();
      const r = host.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      return {{x, y, ray: h.rayAt(x, y), onCanvas: host.contains(document.elementFromPoint(x, y))}};
    }})()""")
    # the ray through the centre pixel runs from the camera - not from a clipping plane the camera
    # left behind when it moved - toward the focal point
    d = aim["ray"]["dir"]
    L = math.sqrt(sum(v * v for v in d))
    assert _near(aim["ray"]["origin"], [ox - 0.25, oy, oz + 0.12], 1e-6), aim
    assert _near([v / L for v in d], [0.3 / math.hypot(0.3, 0.12), 0.0, -0.12 / math.hypot(0.3, 0.12)], 1e-3), aim
    assert aim["onCanvas"] is True, aim
    live.click(aim["x"], aim["y"])
    live.wait_for(f"window._vdbg['gstage:{sid}'].openings().length === 1", timeout=10, what="the click to add an opening")
    out = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}'), tr = root.querySelector('.gc-table tbody tr');
      return {{armed: root.querySelector('.gc-add').classList.contains('armed'),
               size: tr.querySelector('.gc-dim').textContent.trim(), sure: tr.querySelector('.gc-conf').textContent.trim()}};
    }})()""")
    assert out["armed"] is False, out
    assert out["size"].startswith("79.9") and out["sure"] == "you", out
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "a real click on the stage snapping to a hole")


def test_a_step_files_skin_snaps_to_the_cad_faces_exact_numbers(live, tmp_path):
    pytest.importorskip("OCP")
    from types import SimpleNamespace

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.application import geometry_check as gc

    # a thick-walled tube drawn in millimetres, through the CAD scout's own road: its exact faces,
    # the skin it tessellates for the stage, and that skin's holes
    ox, oy, oz = (v * 1000 for v in ORIGIN)
    outer = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(ox, oy, oz), gp_Dir(1, 0, 0)), R_OUT * 1000, LENGTH * 1000).Shape()
    inner = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(ox - 1, oy, oz), gp_Dir(1, 0, 0)), R_IN * 1000, LENGTH * 1000 + 2).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(BRepAlgoAPI_Cut(outer, inner).Shape(), STEPControl_AsIs)
    step = tmp_path / "tube.step"
    writer.Write(str(step))
    facts, skin = gc._scout_exact(step, tmp_path, None, SimpleNamespace(owner_id="t", source_id="t"))
    rings = [f for f in facts["faces"] if f["kind"] == "ring" and abs(f["centroid_mm"][0] - ox) < 1e-6]
    assert len(rings) == 1 and len(facts["holes"]) == 2, (facts["faces"], facts["holes"])

    sid = "open-step"
    _open(live, sid, gc.skin_payload(skin), _check(facts["faces"], facts["holes"]))
    x0, y0, z0 = ORIGIN
    into = _click(live, sid, (x0 - 0.2, y0, z0 + 0.1), (x0 + 0.03, y0 + 0.01, z0))
    # the skin's hole was clicked; the CAD face measured it, so its numbers are the ones
    assert into["end"] is True and into["d"] == rings[0]["diameter_mm"], (into, rings)
    assert into["at"] == rings[0]["centroid_mm"] and _near(into["n"], [-1.0, 0.0, 0.0], 1e-6), (into, rings)
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "the stage snapping on a STEP file's skin")
