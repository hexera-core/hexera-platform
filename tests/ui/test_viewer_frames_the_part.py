# Responsibility: Verify a CFD mesh opens framed on the part, not on a ground plane many body lengths across, with the ground still drawn, the whole part on screen in a tall canvas, and "Fit all" taking in everything shown.
# Boundaries: a body box and a ground square stand in for an external case's wall patches; only the network is stood in for. It reads the camera and projects the part's corners itself, never pixels.
from __future__ import annotations

import base64
import json
import math
import struct

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

JOB = "frame-job"
# the body: a 3 x 1 x 1 box standing on the ground at z = 0; corner index = x + 2y + 4z
BODY = [(x, y, z) for z in (0.0, 1.0) for y in (0.0, 1.0) for x in (0.0, 3.0)]


def _patch(name: str, pts: list, quads: list) -> dict:
    flat = [c for p in pts for c in p]
    polys = [v for q in quads for v in (4, *q)]
    return {"name": name, "type": "wall", "face_count": len(quads),
            "points_b64": base64.b64encode(struct.pack(f"<{len(flat)}f", *flat)).decode(),
            "polys_b64": base64.b64encode(struct.pack(f"<{len(polys)}I", *polys)).decode()}


def _payload(ground: bool = True) -> dict:
    quads = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    patches = [_patch("body", BODY, quads)]
    if ground:
        h = 30.0
        patches.append(_patch("ground", [(-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0)], [(0, 1, 2, 3)]))
    return {"kind": "mesh", "mesh_units": "m", "is_mesh": True, "cell_count": 1000, "parts": [],
            "patches": patches, "quality": {"cells": 1000, "engine": "snappy", "units": "m"}}


def _open(live, payload: dict, width: str = "") -> None:
    live.evaluate("""(async () => {
      document.getElementById('stage').style.width = __W__;
      const PAYLOAD = __P__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__J__/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__J__', document.getElementById('stage'), {metrics: {pass: true}});
    })()""".replace("__W__", json.dumps(width)).replace("__P__", json.dumps(payload)).replace("__J__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}'] && window._vdbg['{JOB}'].size()[0] > 0", timeout=90,
                  what="the viewer to open")


def _camera(live) -> dict:
    return live.evaluate(f"""(() => {{ const d = window._vdbg['{JOB}'], v = d.view();
      return {{dist: d.dist(), focal: v.focal, position: v.position, up: d.up(), size: d.size(),
               fitAll: !document.getElementById('v-fitall-{JOB}').hidden}}; }})()""")


def _on_screen(cam: dict) -> bool:
    # every body corner inside the view, by a pinhole camera at vtk's 30 degree vertical view angle
    f, p, up = cam["focal"], cam["position"], cam["up"]
    fwd = [f[i] - p[i] for i in range(3)]
    n = math.hypot(*fwd)
    fwd = [c / n for c in fwd]
    right = [fwd[1] * up[2] - fwd[2] * up[1], fwd[2] * up[0] - fwd[0] * up[2], fwd[0] * up[1] - fwd[1] * up[0]]
    rn = math.hypot(*right)
    right = [c / rn for c in right]
    upv = [right[1] * fwd[2] - right[2] * fwd[1], right[2] * fwd[0] - right[0] * fwd[2],
           right[0] * fwd[1] - right[1] * fwd[0]]
    t = math.tan(math.radians(15))
    w, h = cam["size"]
    for c in BODY:
        d = [c[i] - p[i] for i in range(3)]
        z = sum(d[i] * fwd[i] for i in range(3))
        if z <= 0:
            return False
        x = sum(d[i] * right[i] for i in range(3)) / (z * t * w / h)
        y = sum(d[i] * upv[i] for i in range(3)) / (z * t)
        if abs(x) > 1.0 or abs(y) > 1.0:
            return False
    return True


def test_a_cfd_mesh_opens_on_the_part_and_fit_all_takes_in_the_ground(live):
    # dev-areen 2026-10-30 (Ahmed body, job f5385d0f): the view opened on the ground plane and the
    # body was a speck in the middle of it
    _open(live, _payload())
    cam = _camera(live)
    assert 0 <= cam["focal"][0] <= 3 and 0 <= cam["focal"][1] <= 1, cam
    assert cam["fitAll"] and _on_screen(cam), cam
    live.evaluate(f"document.getElementById('v-fitall-{JOB}').click()")
    wide = _camera(live)
    assert wide["dist"] > 5 * cam["dist"], (cam, wide)
    # Fit to screen comes back to the part
    live.evaluate(f"document.getElementById('v-fitbtn-{JOB}').click()")
    back = _camera(live)
    assert abs(back["dist"] - cam["dist"]) < 0.05 * cam["dist"], (cam, back)
    assert_clean(live, "a CFD mesh framed on the part")


def test_the_whole_part_is_on_screen_in_a_tall_canvas(live):
    # dev-areen 2026-10-30 (job c92c4240): in the result page's tall, narrow canvas the framed body ran
    # off both sides - vtk fits the part's sphere to the view's height only
    try:
        _open(live, _payload(), width="380px")
        live.evaluate(f"document.getElementById('v-fitbtn-{JOB}').click()")
        cam = _camera(live)
    finally:
        live.evaluate("document.getElementById('stage').style.width = ''")
    assert cam["size"][1] > cam["size"][0], cam     # the case under test: taller than wide
    assert _on_screen(cam), cam
    assert_clean(live, "a part framed in a tall canvas")


def test_a_mesh_with_no_ground_has_no_fit_all(live):
    _open(live, _payload(ground=False))
    assert not _camera(live)["fitAll"]
    assert_clean(live, "a mesh with no ground")
