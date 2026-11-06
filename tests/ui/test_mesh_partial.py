# Responsibility: Verify the mesh-so-far in a real browser - one draw per pointer however often the
# durable log replays it, a route with nothing to serve leaving the screen exactly as it was, a
# frame that never reads as the delivered mesh, a camera whose direction survives the swap, a frame
# that resolves after the page moved on drawing nothing, and a closed partial handing the page back.
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


def test_a_frame_that_resolves_after_a_new_upload_draws_nothing(live):
    # THE STALE FRAME. A user opens a running job by link (the page carries the run, and no
    # conversation of its own), then uploads new geometry while the frame is still in flight. The
    # frame that resumes afterwards belongs to what the page was showing, not to what it is showing
    # now: it may not take the workbench, and it may not hide the new geometry check behind the
    # previous run's mesh. Its pointer is given back, so the frame is still drawable if the page
    # comes back to that run.
    out = live.evaluate(_script("""
      const quick = window.fetch;                 // the route answers slowly enough to move under
      window.fetch = (u, o) => String(u).includes('/mesh/partial')
        ? settle(900).then(() => quick(u, o)) : quick(u, o);
      pointer(__KEY__);
      await settle(150);
      setState.sessionId('session-after-the-upload');      // the upload opened a new conversation
      await settle(2500);
      const stale = {calls: window.__partialCalls.length, drawn: !!drawn(),
                     panels: document.querySelectorAll('.mp-view').length,
                     wb: document.getElementById('app').classList.contains('wb'),
                     toggle: !document.getElementById('wb-toggle').hidden};
      // the pointer was not spent: back on the run it was published for, the same frame draws
      setState.sessionId(null);
      pointer(__KEY__);
      await settle(2500);
      return {stale, back: {calls: window.__partialCalls.length, actors: drawn() && drawn().actors(),
                            panels: document.querySelectorAll('.mp-view').length}};
    """, [_partial()]), timeout=120)
    assert out["stale"] == {"calls": 1, "drawn": False, "panels": 0, "wb": False, "toggle": False}, out
    assert out["back"] == {"calls": 2, "actors": 1, "panels": 1}, out
    assert_clean(live, "a frame that resolved after a new upload")


def test_a_frame_that_resolves_after_the_page_moved_to_another_run_draws_nothing(live):
    # the same rule for the other half of the identity: the run the frame was requested for is no
    # longer the run on screen
    out = live.evaluate(_script("""
      const quick = window.fetch;
      window.fetch = (u, o) => String(u).includes('/mesh/partial')
        ? settle(900).then(() => quick(u, o)) : quick(u, o);
      pointer(__KEY__);
      await settle(150);
      setState.jobId('another-run');              // a second run took the page
      await settle(2500);
      return {calls: window.__partialCalls.length, drawn: !!drawn(),
              panels: document.querySelectorAll('.mp-view').length,
              wb: document.getElementById('app').classList.contains('wb')};
    """, [_partial()]), timeout=120)
    assert out == {"calls": 1, "drawn": False, "panels": 0, "wb": False}, out
    assert_clean(live, "a frame that resolved after another run took the page")


def test_closing_a_partial_on_a_failed_run_hands_the_page_back(live):
    # A RUN THAT FAILS AFTER A FRAME WAS SHOWN delivers no viewer at all - the repair panel draws in
    # the conversation, or has nothing to hand over - so the frame's takeover of the workbench has
    # to come undone with it: no empty workbench, and no "Hide conversation" button left owning a
    # page that has no drawer. The path is the real one: the stage's own ending, through main.js.
    out = live.evaluate(_script("""
      pointer(__KEY__);
      await settle(2500);
      const took = {wb: document.getElementById('app').classList.contains('wb'),
                    panels: document.querySelectorAll('.mp-view').length,
                    toggle: !document.getElementById('wb-toggle').hidden};
      Stage.final({pass: false, attempts: 1, text: 'it failed', files: []});
      await settle(500);
      const tg = document.getElementById('wb-toggle'), app = document.getElementById('app');
      const after = {wb: app.classList.contains('wb'), collapsed: app.classList.contains('wb-collapsed'),
                     panels: document.querySelectorAll('.mp-view').length,
                     viewers: document.querySelectorAll('#workbench .viewer').length,
                     toggle: !tg.hidden, label: tg.textContent.trim(),
                     pressed: tg.getAttribute('aria-pressed')};
      tg.click();                        // nothing is left for it to toggle
      return {took, after, stuck: app.classList.contains('wb-collapsed')};
    """, [_partial()]), timeout=120)
    assert out["took"] == {"wb": True, "panels": 1, "toggle": True}, out
    assert out["after"] == {"wb": False, "collapsed": False, "panels": 0, "viewers": 0,
                            "toggle": False, "label": "Hide conversation", "pressed": "false"}, out
    assert out["stuck"] is False, out
    assert_clean(live, "a partial closed on a failed run")


def test_a_cancelled_run_leaves_no_empty_workbench(live):
    # cancelling is the other ending with no viewer behind it
    out = live.evaluate(_script("""
      pointer(__KEY__);
      await settle(2500);
      const before = document.querySelectorAll('.mp-view').length;
      Stage.final({pass: false, cancelled: true, attempts: 1, text: 'cancelled', files: []});
      await settle(500);
      const tg = document.getElementById('wb-toggle');
      return {before, panels: document.querySelectorAll('.mp-view').length,
              wb: document.getElementById('app').classList.contains('wb'),
              viewers: document.querySelectorAll('#workbench .viewer').length,
              toggle: !tg.hidden};
    """, [_partial()]), timeout=120)
    assert out == {"before": 1, "panels": 0, "wb": False, "viewers": 0, "toggle": False}, out
    assert_clean(live, "a partial closed on a cancelled run")
