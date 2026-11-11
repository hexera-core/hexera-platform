# Responsibility: Verify in a real browser that clipping leaves the camera aimed at what SURVIVES
# the clip, so zooming afterwards converges on mesh rather than on the clip plane.
# Boundaries: the payload is the one the backend builds; only the network is stood in for.
from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "clip-job"

# WHY THIS TEST EXISTS. `resetCamera` puts the focal point at the centre of the bounding box and
# `applyClip` puts the plane at the midpoint of that same box, so at the slider's default the two
# were THE SAME POINT. The camera dollies toward its focal point, so zooming after a clip converged
# the camera onto the clip plane itself - and everything past the plane is precisely what was
# removed. Measured in a browser on the delivered coolant, canvas drawn against wheel clicks:
#
#       clicks      no clip     clip on z
#            0       16.26%        11.66%
#           20       83.63%        33.37%
#           40      100.00%         3.23%
#
# A reader clipped to see inside, zoomed to see detail, and watched the mesh evaporate. After the
# fix the clipped run rises to 98% instead of collapsing. Pixels are not available here, so what
# this holds is the cause: where the clip leaves the camera pointing.


def _slab(name: str, n: int = 8, h: float = 0.01) -> dict:
    """A row of quads along z, so a clip on z has a clear near half and far half."""
    pts, quads = [], []
    for i in range(n + 1):
        z = i * h
        pts += [[0.0, 0.0, z], [h, 0.0, z]]
    for i in range(n):
        a = i * 2
        quads.append([4, a, a + 1, a + 3, a + 2])
    return {"name": name, "type": "wall", "face_count": n,
            "points": np.asarray(pts, dtype=np.float32).tobytes(),
            "polys": np.asarray(quads, dtype=np.uint32).reshape(-1).tobytes()}


def _payload() -> dict:
    surf = polymesh_response([_slab("wall")], {"p10": 0.01, "typ": 0.01}, {"wall": "wall"}, "m")
    surf.update(is_mesh=True, parts=[], quality={"engine": "cfmesh_conjugate", "units": "m"})
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
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}']", timeout=90,
                  what="the viewer to initialise")


def _clip(live, axis: str, pos: int = 50) -> None:
    live.evaluate(f"""(() => {{
      const b = [...document.querySelectorAll('#v-insp-box-{JOB} .v-insp-c[data-ax]')]
          .find(e => e.dataset.ax === '{axis}');
      if (b) b.click();
      const sl = document.getElementById('v-insp-clip-{JOB}');
      if (sl && !sl.disabled) {{ sl.value = {pos};
        sl.dispatchEvent(new Event('input', {{bubbles: true}})); }}
      return 1; }})()""")


def _focal(live):
    return live.evaluate(f"window._vdbg['{JOB}'].view().focal")


def test_clipping_aims_the_camera_at_the_half_that_survives(live):
    _open(live, _payload())
    before = _focal(live)
    _clip(live, "z", 50)
    after = _focal(live)
    assert live.evaluate(f"window._vdbg['{JOB}'].detail().clip") == "z"
    # the slab runs z = 0 .. 0.08; the plane lands at 0.04 and the kept half is the far one,
    # so the focal point must move INTO it rather than sit on the plane
    assert after[2] > before[2] + 1e-6, (
        "the focal point did not move off the clip plane: before %r after %r" % (before, after))
    assert after[2] > 0.04 + 1e-9, "it is still on or behind the plane at %r" % (after,)
    # and only along the clip axis - a clip must not swing the view sideways
    assert after[0] == pytest.approx(before[0], abs=1e-9)
    assert after[1] == pytest.approx(before[1], abs=1e-9)
    assert_clean(live, "the viewer after a clip")


def test_the_camera_keeps_its_direction_and_distance(live):
    """Re-aiming moves the focal point AND the camera by the same vector, so the reader's view
    does not swing - only what the zoom converges on changes."""
    _open(live, _payload())
    v0 = live.evaluate(f"window._vdbg['{JOB}'].view()")
    d0 = live.evaluate(f"window._vdbg['{JOB}'].dist()")
    _clip(live, "z", 50)
    v1 = live.evaluate(f"window._vdbg['{JOB}'].view()")
    d1 = live.evaluate(f"window._vdbg['{JOB}'].dist()")
    assert d1 == pytest.approx(d0, rel=1e-6), "the clip changed how far away the camera is"
    for k in range(3):
        assert v1["dir"][k] == pytest.approx(v0["dir"][k], abs=1e-6), "the clip swung the view"


def test_flipping_the_clip_aims_at_the_other_half(live):
    _open(live, _payload())
    _clip(live, "z", 50)
    far = _focal(live)
    live.evaluate(f"(() => {{ const b = document.getElementById('v-insp-flip-{JOB}');"
                  f" if (b) b.click(); return !!b; }})()")
    near = _focal(live)
    assert near[2] < far[2] - 1e-6, (
        "flip kept the camera aimed at the half that was just removed: %r then %r" % (far, near))


def test_turning_the_clip_off_puts_the_aim_back(live):
    _open(live, _payload())
    before = _focal(live)
    _clip(live, "z", 50)
    assert _focal(live)[2] > before[2] + 1e-6
    live.evaluate(f"""(() => {{
      const b = [...document.querySelectorAll('#v-insp-box-{JOB} .v-insp-c')]
          .find(e => (e.textContent || '').trim().toLowerCase() === 'off');
      if (b) b.click(); return !!b; }})()""")
    back = _focal(live)
    assert back[2] == pytest.approx(before[2], abs=1e-6), (
        "the aim stayed where the clip left it after the clip was turned off: %r" % (back,))
    assert live.evaluate(f"window._vdbg['{JOB}'].detail().clip") in (None, "", False)


def test_a_cut_does_not_move_the_camera(live):
    """`applyClip` is called after every cut and from `clear`. Re-aiming on those would be the
    camera wandering off on its own, which is why only the clip CONTROLS call it."""
    _open(live, _payload())
    _clip(live, "z", 50)
    at = _focal(live)
    live.evaluate(f"(() => {{ window._vdbg['{JOB}'].detail().arm(true);"
                  f" window._vdbg['{JOB}'].detail().arm(false); return 1; }})()")
    assert _focal(live) == pytest.approx(at, abs=1e-12)
