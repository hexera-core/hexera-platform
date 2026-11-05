# Responsibility: Verify the mesh-so-far in a real browser - one draw per pointer however often the
# durable log replays it, a route with nothing to serve leaving the screen exactly as it was, a
# frame that never reads as the delivered mesh, and a camera whose direction survives the swap.
# Boundaries: the console's own modules in the page, fed through the one event dispatch a live or
# replayed run goes through; only the partial route is stood in for. It reads the camera, never pixels.
from __future__ import annotations

import base64
import json
import struct

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

JOB = "partial-job"
DBG = "partial:" + JOB
KEY = "jobs/op-1/partial-castellated.json"
KEY_2 = "jobs/op-1/partial-snapped.json"


def _box(size: float) -> dict:
    """One axis-aligned box as a boundary patch - six quads, the shape a frame of a mesh has."""
    pts = [(x, y, z) for z in (0.0, size) for y in (0.0, size) for x in (0.0, size)]
    quads = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    flat = [c for p in pts for c in p]
    polys = [v for q in quads for v in (4, *q)]
    return {"name": "part", "type": "wall", "face_count": len(quads),
            "points_b64": base64.b64encode(struct.pack(f"<{len(flat)}f", *flat)).decode(),
            "polys_b64": base64.b64encode(struct.pack(f"<{len(polys)}I", *polys)).decode()}


def _partial(size: float = 1.0) -> dict:
    # exactly what the worker publishes: the viewer's own surface shape, marked as a partial
    return {"kind": "polymesh", "mesh_units": "m", "patches": [_box(size)],
            "is_mesh": False, "partial": True, "stage": "castellated", "cell_stats": None}


# The page is the real one: main.js has already installed the handler that draws a pointer, so the
# event travels the path it travels in production. Only the route is stood in for, and every call
# to it is counted - that count is what "fetched once" is asserted on. The Nth call is answered
# with the Nth payload (the last one repeats); a null payload is answered __STATUS__.
SETUP = """
  const { Stage } = await import('/static/js/render/stage.js');
  const { dispatch } = await import('/static/js/core/events.js');
  const { set: setState } = await import('/static/js/core/state.js');
  Stage.mount(); Stage.ensureProc();
  setState.jobId(__JOB__);
  window.__partialCalls = [];
  const PAYLOADS = __PAYLOADS__;
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u);
    if (s.includes('/mesh/partial')) {
      window.__partialCalls.push(s);
      const body = PAYLOADS[Math.min(window.__partialCalls.length - 1, PAYLOADS.length - 1)];
      if (body === null) return Promise.resolve(new Response('{"detail":"none"}',
        {status: __STATUS__, headers: {'Content-Type': 'application/json'}}));
      return Promise.resolve(new Response(JSON.stringify(body),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return real(u, o);
  };
  let cur = null;
  const ev = (e) => { cur = dispatch(e, Stage, cur); };
  const pointer = (key) => ev({type: 'mesh_partial', stage: 'builder', phase: 'castellated',
                              object_key: key});
  const drawn = () => (window._vdbg || {})[__DBG__] || null;
  const settle = (ms) => new Promise((r) => setTimeout(r, ms));
"""


def _script(body: str, payloads: list, status: int = 404) -> str:
    out = "(async () => {" + SETUP + body + "})()"
    for token, value in (("__PAYLOADS__", json.dumps(payloads)), ("__STATUS__", str(status)),
                         ("__JOB__", json.dumps(JOB)), ("__DBG__", json.dumps(DBG)),
                         ("__KEY__", json.dumps(KEY)), ("__KEY2__", json.dumps(KEY_2))):
        out = out.replace(token, value)
    return out


def test_the_same_pointer_is_fetched_once_and_drawn_once(live):
    # the durable event log is replayed on every reconnect and on every page load, so the same
    # pointer arrives again and again; it may not stack a second set of actors or re-read the route
    out = live.evaluate(_script("""
      pointer(__KEY__); pointer(__KEY__);           // the replay, before the first read answers
      await settle(2500);
      pointer(__KEY__);                             // and again, after it is on screen
      await settle(400);
      return {calls: window.__partialCalls.length, actors: drawn() && drawn().actors(),
              panels: document.querySelectorAll('.mp-view').length};
    """, [_partial()]), timeout=120)
    assert out == {"calls": 1, "actors": 1, "panels": 1}, out
    assert_clean(live, "the mesh-so-far drawn once for one pointer")


def test_a_route_with_nothing_to_serve_leaves_the_view_alone(live):
    # 404: the flag is off, nothing is published yet, or the exchange was tidied. 503: the exchange
    # could not be read. Either way the screen keeps what it had and the page does not throw.
    for status in (404, 503):
        out = live.evaluate(_script("""
          pointer(__KEY__ + __STATUS__);   // a key of its own, so the dedupe is not what answers
          await settle(600);
          return {panels: document.querySelectorAll('.mp-view').length, drawn: !!drawn(),
                  calls: window.__partialCalls.length};
        """, [None], status=status), timeout=60)
        assert out == {"panels": 0, "drawn": False, "calls": 1}, (status, out)
    assert_clean(live, "a partial the server has none of")


def test_a_partial_is_never_the_delivered_mesh(live):
    out = live.evaluate(_script("""
      pointer(__KEY__);
      await settle(2500);
      const box = document.getElementById('mesh-partial-' + __JOB__);
      return {panel: !!box,
              caption: box ? box.querySelector('.v-count').textContent : '',
              downloads: box ? box.querySelectorAll('.v-dl,.v-dlbtn,.rp-dl').length : -1,
              result: !!document.getElementById('viewer-' + __JOB__),
              verdicts: document.querySelectorAll('.rp-verdict,.rp-head').length,
              finished: !!Stage.finished};
    """, [_partial()]), timeout=120)
    assert out["panel"] and out["downloads"] == 0, out
    assert out["result"] is False and out["verdicts"] == 0 and out["finished"] is False, out
    # the words say which mesh this is, and no cell count is claimed for it
    assert "Mesh being built" in out["caption"] and "not the finished mesh" in out["caption"], out
    assert "cells" not in out["caption"], out
    assert_clean(live, "a partial that is not the delivered mesh")


def test_the_camera_keeps_its_direction_across_a_swap(live):
    # THE WHOLE POINT. A frame of a growing mesh has its own bounds and may be in another unit, so
    # the promise is the honest one: the viewing direction and the up vector are kept and the new
    # content is re-framed against its own bounds (design 2026-11-04 section 4).
    out = live.evaluate(_script("""
      pointer(__KEY__);
      await settle(2500);
      const d = drawn(), cam = d.cam();
      // the user turns the part and zooms: nothing below may undo this direction
      cam.setFocalPoint(0.5, 0.5, 0.5);
      cam.setPosition(0.5, 0.5, 4.5);
      cam.setViewUp(0, 1, 0);
      d.render();
      const before = {...d.view(), dist: d.dist(), actors: d.actors()};
      pointer(__KEY2__);
      await settle(2500);
      return {before, after: {...d.view(), dist: d.dist(), actors: d.actors()},
              calls: window.__partialCalls.length,
              panels: document.querySelectorAll('.mp-view').length};
    """, [_partial(1.0), _partial(5.0)]), timeout=120)
    before, after = out["before"], out["after"]
    assert out["calls"] == 2 and out["panels"] == 1, out
    # the direction of projection and the up vector are untouched by the swap
    for i in range(3):
        assert abs(before["dir"][i] - after["dir"][i]) < 1e-6, out
        assert abs(before["up"][i] - after["up"][i]) < 1e-6, out
    # and the new content is framed, not left at the old distance: a part five times the size
    # needs the camera further back
    assert after["dist"] > 3 * before["dist"], out
    # one content, swapped - not two sets of actors
    assert before["actors"] == 1 and after["actors"] == 1, out
    assert_clean(live, "the camera across a partial swap")
