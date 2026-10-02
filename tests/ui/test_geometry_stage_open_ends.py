# Responsibility: Verify "Add an opening" lands on the open end the user clicks near, from the
# part's triangles alone: a click into the hole of a thin-walled tube snaps to its open rim, a
# click into the hole of a thick-walled tube - or on the flat ring around it - snaps to the ring's
# inner loop, a click on the wall close to an end snaps to that end, and a click far from any end
# falls back to the clicked spot with a note; the snapped centre, size and direction reach the
# confirmation. A STEP file's tessellation snaps the same way, to the CAD face's exact numbers.
# Boundaries: the skin is built through the worker's own code from geometry made here; only the
# network is stood in for. The rays are given in the part's coordinates (one test drives a real
# click on the canvas, through the camera).
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


def _check(faces: list | None = None) -> dict:
    # the measuring step found no opening (the aorta's case), so the user adds them
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "tube", "input_kind": "body-surface", "flow": "internal",
        "size_mm": [300.0, 100.0, 100.0], "seed_point_mm": [250.0, 200.0, 300.0], "notes": [],
        "openings": [], "faces": faces or []}}


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


def _add(live, sid: str, origin, toward) -> dict:
    """Add an opening along the ray from `origin` toward a point, as a click there does; the new
    opening, its row's size and sureness, and the hint beside the button."""
    return live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{sid}'], root = document.getElementById('gstage-{sid}');
      const o = {json.dumps(list(origin))}, t = {json.dumps(list(toward))};
      const a = h.addAt({{origin: o, dir: [t[0] - o[0], t[1] - o[1], t[2] - o[2]]}});
      if (!a) return null;
      const tr = root.querySelector('tr[data-id="' + a.id + '"]');
      return {{id: a.id, kind: a.kind, shape: a.shape, end: !!a.open_end, snapped: !!a.snapped, d: a.diameter_mm,
               at: a.centroid_mm, n: a.normal, size: tr.querySelector('.gc-dim').textContent.trim(),
               box: !!tr.querySelector('.gc-dia'), sure: tr.querySelector('.gc-conf').textContent.trim(),
               hint: root.querySelector('.gc-tools-hint').textContent}};
    }})()""")


def _near(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_a_click_into_a_thin_walled_tubes_open_end_snaps_to_the_rim(live):
    sid = "open-thin"
    _open(live, sid, _skin(_tube(R_OUT, None)), _check())
    ox, oy, oz = ORIGIN
    # from outside, slanting into the hole at x = 0: the ray passes through the hole, which has no
    # surface, and meets the wall inside - the spot the old picking put the sticker on
    origin, toward = (ox - 0.2, oy, oz + 0.1), (ox + 0.03, oy + 0.01, oz)
    hit = live.evaluate(f"""(async () => {{
      const {{ openEnds, snapAt }} = await import('/static/js/viewer/open_ends.js');
      const skin = {json.dumps(_skin(_tube(R_OUT, None)))};
      const dec = (b, T) => {{ const s = atob(b), u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return new T(u.buffer); }};
      const ends = openEnds(skin.patches.map((p) => ({{pts: dec(p.points_b64, Float32Array), polys: dec(p.polys_b64, Uint32Array)}})));
      const o = {json.dumps(list(origin))}, t = {json.dumps(list(toward))};
      const s = snapAt(ends, {{origin: o, dir: [t[0] - o[0], t[1] - o[1], t[2] - o[2]]}});
      return {{kinds: ends.openings.map((e) => e.kind), hitX: s.hit && s.hit.point[0], through: s.through && s.through.kind}};
    }})()""")
    assert hit["kinds"] == ["rim", "rim"], hit
    assert hit["through"] == "rim" and hit["hitX"] > ox + 0.05, hit        # the wall it met is well inside

    o = _add(live, sid, origin, toward)
    assert o["end"] is True and o["kind"] == "rim" and o["shape"] == "circle", o
    assert _near(o["at"], [ox * 1000, oy * 1000, oz * 1000], 0.05), o
    assert abs(o["d"] - _equivalent_mm(R_OUT)) <= 0.05 and abs(o["d"] - 100.0) < 0.5, o
    assert _near(o["n"], [-1.0, 0.0, 0.0], 1e-4), o                       # out of the part
    assert o["size"].startswith("99.9") and o["box"] is False and o["sure"] == "you", o
    assert "open end" in o["hint"], o
    # the other end, from the far side: the same, facing +x
    far = _add(live, sid, (ox + LENGTH + 0.2, oy, oz - 0.1), (ox + LENGTH - 0.03, oy, oz + 0.01))
    assert far["end"] is True and _near(far["at"], [(ox + LENGTH) * 1000, oy * 1000, oz * 1000], 0.05), far
    assert _near(far["n"], [1.0, 0.0, 0.0], 1e-4), far
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "the stage snapping to a thin wall's open end")


def test_a_thick_walled_tube_snaps_to_the_rings_inner_loop_and_falls_back_far_from_any_end(live):
    sid = "open-thick"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check())
    ox, oy, oz = ORIGIN
    inner_mm = _equivalent_mm(R_IN)
    end0 = [ox * 1000, oy * 1000, oz * 1000]
    # INTO THE HOLE: the ray meets the bore's wall behind it, and the opening is the inner loop
    into = _add(live, sid, (ox - 0.2, oy, oz + 0.1), (ox + 0.03, oy + 0.01, oz))
    assert into["end"] is True and into["kind"] == "ring", into
    assert abs(into["d"] - inner_mm) <= 0.05 and into["d"] < 81.0, into        # the hole, not the wall's outside
    assert _near(into["at"], end0, 0.05) and _near(into["n"], [-1.0, 0.0, 0.0], 1e-4), into
    # ON THE RING ITSELF, between the two loops: the same inner loop
    rim = _add(live, sid, (ox - 0.2, oy + 0.045, oz + 0.05), (ox, oy + 0.045, oz))
    assert rim["end"] is True and abs(rim["d"] - inner_mm) <= 0.05 and _near(rim["at"], end0, 0.05), rim
    # ON THE OUTER WALL 5 mm from the end, from above: no plane is looked through, but the end is
    # right there - the nearest open end
    beside = _add(live, sid, (ox + 0.005, oy, oz + 0.4), (ox + 0.005, oy, oz + R_OUT))
    assert beside["end"] is True and abs(beside["d"] - inner_mm) <= 0.05 and _near(beside["at"], end0, 0.05), beside
    # FAR FROM ANY END, the middle of the tube: today's behaviour - the clicked spot, no size,
    # and a note that no open end was found there
    mid = _add(live, sid, (ox + 0.15, oy, oz + 0.4), (ox + 0.15, oy, oz + R_OUT))
    assert mid["end"] is False and mid["snapped"] is False and mid["shape"] == "unknown" and mid["box"] is True, mid
    assert _near(mid["at"], [(ox + 0.15) * 1000, oy * 1000, (oz + R_OUT) * 1000], 0.05), mid
    assert "No open end found there" in mid["hint"], mid
    # nothing at all along the ray: nothing is added
    assert _add(live, sid, (ox + 0.15, oy + 0.3, oz + 0.4), (ox + 0.15, oy + 0.3, oz)) is None

    # PROCEED: the snapped openings reach the confirmation with their centre, size and direction
    live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('tr[data-id="{mid["id"]}"] .gc-x').click();
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
    assert_clean(live, "the stage snapping to a thick wall's end ring")


def test_a_real_click_on_the_canvas_into_the_hole_snaps_through_the_camera(live):
    sid = "open-click"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check())
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
    assert_clean(live, "a real click on the stage snapping to an open end")


def test_a_step_files_tessellation_snaps_to_the_cad_faces_exact_numbers(live, tmp_path):
    pytest.importorskip("OCP")
    from types import SimpleNamespace

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.application import geometry_check as gc

    # a thick-walled tube drawn in millimetres, through the CAD scout's own road: its exact faces,
    # and the skin it tessellates for the stage
    ox, oy, oz = (v * 1000 for v in ORIGIN)
    outer = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(ox, oy, oz), gp_Dir(1, 0, 0)), R_OUT * 1000, LENGTH * 1000).Shape()
    inner = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(ox - 1, oy, oz), gp_Dir(1, 0, 0)), R_IN * 1000, LENGTH * 1000 + 2).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(BRepAlgoAPI_Cut(outer, inner).Shape(), STEPControl_AsIs)
    step = tmp_path / "tube.step"
    writer.Write(str(step))
    facts, skin = gc._scout_exact(step, tmp_path, None, SimpleNamespace(owner_id="t", source_id="t"))
    rings = [f for f in facts["faces"] if f["kind"] == "ring" and abs(f["centroid_mm"][0] - ox) < 1e-6]
    assert len(rings) == 1, facts["faces"]

    sid = "open-step"
    _open(live, sid, gc.skin_payload(skin), _check(facts["faces"]))
    x0, y0, z0 = ORIGIN
    into = _add(live, sid, (x0 - 0.2, y0, z0 + 0.1), (x0 + 0.03, y0 + 0.01, z0))
    # the coarse tessellation found the end; the CAD face measured it, so its numbers are the ones
    assert into["end"] is True and into["d"] == rings[0]["diameter_mm"], (into, rings)
    assert into["at"] == rings[0]["centroid_mm"] and _near(into["n"], [-1.0, 0.0, 0.0], 1e-6), (into, rings)
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "the stage snapping on a STEP file's tessellation")
