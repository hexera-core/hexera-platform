# Responsibility: Verify in a real browser that the deep-zoom window asks for the parts on screen,
# aims the zoom at the mesh rather than at the middle of the bounding box, and never draws an
# answer that outlived the question.
# Boundaries: the payload is the one the backend builds; only the network is stood in for.
from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "window-job"

# WHY THIS TEST EXISTS. Deep zoom replaces the drawn surface with the real delivered cells inside
# the box the reader is looking at. Four things went wrong at once and all four were invisible to
# the existing gate, because nothing checked what the client ASKS for:
#
#   - the box was asked for without saying which parts are on screen, so isolating the coolant
#     still drew the metal over it;
#   - and then, because the box is centred on whatever is under the middle of the screen, the
#     next box centred on the metal that had just been drawn in front and walked off the coolant
#     entirely - measured, 400 faces of solid/solid_to_air where the coolant had tens of thousands;
#   - the camera dollies toward its FOCAL POINT, which `resetCamera` leaves at the centre of the
#     bounding box - empty space inside the part. Thirty-two wheel clicks moved the camera 54 mm
#     closer to that point and 4 mm closer to the mesh, so the view across the mesh barely changed
#     and the real cells could never take over however long a reader scrolled;
#   - and an answer in flight outlived `setDeep(false)`: the cells landed after the mode was off,
#     with the surface left hidden under them.
#
# The patch-matching semantics live in tests/unit/render/test_a_window_shows_only_what_the_reader
# _can_see.py. What is held HERE is the client's half of the contract, which only a browser has.

W = 0.02
CENTRE = W / 2.0

_PTS = np.array([[0, 0, 0], [W, 0, 0], [W, W, 0], [0, W, 0],
                 [0, 0, W], [W, 0, W], [W, W, W], [0, W, W]], dtype=np.float32)
_FACES = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4],
          [3, 7, 6, 2], [0, 4, 7, 3], [1, 2, 6, 5]]


def _half(name: str, idx: list[int]) -> dict:
    """Three faces of a CLOSED box, so the two patches together are a shell.

    A closed shell and not a pair of flat sheets, for two reasons. The camera direction after
    `resetCamera` is not something this test gets to choose, and flat sheets are invisible to a
    centre-screen pick from any direction that lies in their plane - which is how the first
    version of this test failed. And a shell is the real arrangement anyway: the thing the aim
    fix exists for is that the centre of the bounding box is EMPTY SPACE inside the part, which
    on the delivered cold plate is a point inside the metal.
    """
    polys: list[int] = []
    for i in idx:
        polys += [4] + _FACES[i]
    return {"name": name, "type": "wall", "face_count": len(idx),
            "points": _PTS.tobytes(),
            "polys": np.asarray(polys, dtype=np.uint32).tobytes()}


def _payload() -> dict:
    surf = polymesh_response([_half("fluid/wall", [0, 2, 4]), _half("solid/wall", [1, 3, 5])],
                             {"p10": 0.002, "typ": 0.002},
                             {"fluid/wall": "wall", "solid/wall": "wall"}, "m")
    for p in surf["patches"]:
        p["poly_count"] = 1
        p["delivered_faces"] = 10_000
    surf["exact"] = {
        "delivered_faces": 20_000, "drawn_polygons": 2, "drawn_triangles": 4,
        "collapse": 10_000.0, "lossless": True, "area_error": 1.0e-12,
        "patches": {"fluid/wall": {"delivered_faces": 10_000, "drawn_polygons": 1,
                                   "area_error": 1.0e-12},
                    "solid/wall": {"delivered_faces": 10_000, "drawn_polygons": 1,
                                   "area_error": 1.0e-12}},
    }
    surf.update(is_mesh=True, parts=[],
                quality={"engine": "cfmesh_conjugate", "units": "m", "boundary_faces": 20_000})
    return surf


def _open(live, payload: dict, *, window: dict | None = None, hold: bool = False) -> None:
    """Mount the viewer with /window stood in for. Every window URL is recorded on
    `window.__win`, and with `hold` the answer waits until the test releases it."""
    body = window if window is not None else {
        "job_id": JOB, "fits": True, "faces_in_box": 1234, "faces_returned": 1234,
        "max_faces": 1_500_000, "patches": [],
    }
    live.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__, WINDOW = __WINDOW__, HOLD = __HOLD__;
      window.__win = [];
      window.__release = null;
      const real = window.fetch.bind(window);
      const json = (b) => new Response(JSON.stringify(b),
        {status: 200, headers: {'Content-Type': 'application/json'}});
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/window?')) {
          window.__win.push(s);
          if (HOLD) return new Promise(res => { window.__release = () => res(json(WINDOW)); });
          return Promise.resolve(json(WINDOW));
        }
        if (s.includes('/simulation/__JOB__/surface')) return Promise.resolve(json(PAYLOAD));
        if (s.includes('/client-config')) return Promise.resolve(json({}));
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
    })()""".replace("__PAYLOAD__", json.dumps(payload))
         .replace("__WINDOW__", json.dumps(body))
         .replace("__HOLD__", "true" if hold else "false")
         .replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}']", timeout=90,
                  what="the viewer to initialise")


def _d(live, expr: str):
    return live.evaluate(f"window._vdbg['{JOB}'].detail().{expr}")


def _urls(live):
    return live.evaluate("window.__win")


# ------------------------------------------------------------------ what the reader can see
def test_deep_is_armed_by_itself_on_an_exact_payload(live):
    """A mode a reader has to find is not an answer to "show me my mesh"."""
    _open(live, _payload())
    live.wait_for(f"window._vdbg['{JOB}'].detail().deepOn()", timeout=30,
                  what="deep to arm itself")
    assert_clean(live, "a viewer that armed deep zoom on its own")


def test_the_keep_list_names_the_patches_on_screen(live):
    _open(live, _payload())
    assert sorted(_d(live, "deepKeep()")) == ["fluid/wall", "solid/wall"]


def test_the_window_is_told_what_to_keep(live):
    """THE DEFECT THIS STARTED FROM. Without a list the route answers with every region in the
    box whatever the reader has turned off, and on a conjugate mesh that is the metal drawn over
    the coolant."""
    _open(live, _payload())
    live.evaluate(f"(() => window._vdbg['{JOB}'].detail().deepAgain())()", timeout=60)
    live.wait_for("window.__win.length > 0", timeout=60, what="a window request")
    url = _urls(live)[-1]
    assert "keep=" in url, "the window was asked for a box and nothing else: %s" % url
    for name in ("fluid%2Fwall", "solid%2Fwall"):
        assert name in url, "%s is on screen and was not kept: %s" % (name, url)


def test_the_box_is_a_box(live):
    """Six corners, all finite, and a positive extent on every axis - the route refuses anything
    else and a refusal is a console error a reader never sees the cause of."""
    _open(live, _payload())
    b = _d(live, "deepBox()")
    for k in range(3):
        assert b["hi"][k] > b["lo"][k], "the box has no extent on axis %d: %r" % (k, b)
    assert b["across"] > 0


# ------------------------------------------------------------------ the aim
def test_the_zoom_is_aimed_at_the_mesh_and_not_at_the_middle_of_the_box(live):
    """`resetCamera` leaves the focal point at the centre of the bounding box, which for a shell
    is empty space INSIDE it. The camera dollies toward its focal point, so it converges on a
    place the mesh is not: measured on the delivered coolant, thirty-two wheel clicks moved the
    camera 54 mm closer to that point and 4 mm closer to the part.

    Aiming slides the focal point along the view direction onto whatever is under the middle of
    the screen. That is the near wall, which is NEARER than the centre, so the distance the
    camera thinks it is working at drops - and `camDist` is what every cell-size reading on this
    page is divided by."""
    _open(live, _payload())
    # Deep arms itself on an exact payload and aims as part of its first pass, so by now the
    # camera is ALREADY aimed. Turn the mode off so nothing re-aims under this test, put the
    # camera back where `resetCamera` leaves it, and then aim once, deliberately.
    live.evaluate(f"(() => {{ window._vdbg['{JOB}'].detail().deep(false); return 1; }})()")
    live.evaluate(f"(() => {{ const b = document.getElementById('v-fitbtn-{JOB}');"
                  f" if (b) b.click(); return !!b; }})()")
    before = live.evaluate(f"window._vdbg['{JOB}'].view().focal")
    for k in range(3):
        assert before[k] == pytest.approx(CENTRE, abs=1e-6), (
            "this fixture only tests anything if the focal point starts inside the shell: %r"
            % (before,))
    d0 = live.evaluate(f"window._vdbg['{JOB}'].dist()")
    moved = live.evaluate(f"(async () => await window._vdbg['{JOB}'].detail().deepAim())()",
                          timeout=60)
    after = live.evaluate(f"window._vdbg['{JOB}'].view().focal")
    d1 = live.evaluate(f"window._vdbg['{JOB}'].dist()")
    assert moved is True, "nothing was under the middle of the screen to aim at"
    assert after != pytest.approx(before, abs=1e-9), (
        "the focal point stayed in the middle of the box at %r" % (after,))
    assert d1 < d0 - 1e-9, (
        "the camera still thinks it is working at the distance of empty space: %.6f then %.6f"
        % (d0, d1))


def test_aiming_does_not_swing_the_view(live):
    """The focal point stays on the SAME RAY, so the view direction, the up vector and therefore
    the rendered image are untouched. Only what the next dolly converges on changes."""
    _open(live, _payload())
    v0 = live.evaluate(f"window._vdbg['{JOB}'].view()")
    live.evaluate(f"(async () => await window._vdbg['{JOB}'].detail().deepAim())()", timeout=60)
    v1 = live.evaluate(f"window._vdbg['{JOB}'].view()")
    for k in range(3):
        assert v1["dir"][k] == pytest.approx(v0["dir"][k], abs=1e-6), "aiming swung the view"


def test_aiming_again_changes_nothing(live):
    """The deadband. `setFocalPoint` fires `onModified`, which pings this mode again - without a
    close-enough test it would re-aim and re-ping for ever."""
    _open(live, _payload())
    live.evaluate(f"(async () => await window._vdbg['{JOB}'].detail().deepAim())()", timeout=60)
    once = live.evaluate(f"window._vdbg['{JOB}'].view().focal")
    again = live.evaluate(f"(async () => await window._vdbg['{JOB}'].detail().deepAim())()",
                          timeout=60)
    twice = live.evaluate(f"window._vdbg['{JOB}'].view().focal")
    assert again is False, "a second aim at the same thing still moved the camera"
    assert twice == pytest.approx(once, abs=1e-12)


# ------------------------------------------------------------------ answers that outlived the ask
def test_an_empty_window_teaches_no_density(live):
    """`depWorth` scales a count by the square of the width, and zero scales to zero at EVERY
    width - so one empty box would say "worth asking" for ever and the gate would be dead. An
    empty box is routine now that a keep list can leave a whole region out."""
    _open(live, _payload(), window={"job_id": JOB, "fits": True, "faces_in_box": 0,
                                    "faces_returned": 0, "max_faces": 1_500_000, "patches": []})
    live.evaluate(f"(() => window._vdbg['{JOB}'].detail().deepAgain())()", timeout=60)
    live.wait_for("window.__win.length > 0", timeout=60, what="a window request")
    assert _d(live, "deepSeen()") == -1, "an empty answer was recorded as this view's density"


def test_a_real_count_is_learnt(live):
    _open(live, _payload())
    live.evaluate(f"(() => window._vdbg['{JOB}'].detail().deepAgain())()", timeout=60)
    live.wait_for(f"window._vdbg['{JOB}'].detail().deepSeen() >= 0", timeout=60,
                  what="the route's count to be learnt")
    assert _d(live, "deepSeen()") == 1234
    assert _d(live, "deepCap()") == 1_500_000


def test_an_answer_that_outlived_the_mode_is_not_drawn(live):
    """`setDeep(false)` runs `clear()`, and then the fetch in flight landed, drew its cells and
    hid the surface again: Deep off, cells up, part gone."""
    _open(live, _payload(), hold=True)
    live.evaluate(f"(() => {{ window._vdbg['{JOB}'].detail().deepAgain(); return 1; }})()")
    live.wait_for("window.__win.length > 0", timeout=60, what="a window request in flight")
    live.evaluate(f"(() => {{ window._vdbg['{JOB}'].detail().deep(false); return 1; }})()")
    live.evaluate("(() => { if (window.__release) window.__release(); return 1; })()")
    live.wait_for(f"!window._vdbg['{JOB}'].detail().button.dataset.busy", timeout=60,
                  what="the late answer to be dealt with")
    assert _d(live, "deepOn()") is False
    assert _d(live, "shown") == 0, "a late answer drew its cells after the mode was off"
    assert _d(live, "hidden") == 0, "the surface was left hidden with deep off"
    assert_clean(live, "a viewer that dropped a late window")
