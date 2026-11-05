# Responsibility: Prove the shared scene boot in a real browser - one render window per host with
#                 the three-light rig on it, one animation frame loop for every anchor, and a
#                 dispose() that actually gives the WebGL context back.
# Boundaries: no job, no payload and no API; it mounts the module on a bare div. It asserts the
#             module's own behaviour, never pixels.
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

REPO = Path(__file__).parents[2]

# The scene's host: positioned, sized, and in the document, which is all mountCanvas asks of it.
_MOUNT = """(async () => {
  const { loadVtk } = await import('/static/js/viewer/scene/vtk_load.js');
  await loadVtk();
  const { mountCanvas } = await import('/static/js/viewer/scene/canvas.js');
  const { makeOverlay } = await import('/static/js/viewer/scene/overlay.js');
  const host = document.createElement('div');
  host.id = 'scene-host';
  host.style.cssText = 'position:relative;width:400px;height:300px';
  document.body.appendChild(host);
  const canvas = mountCanvas(host, {background: [0, 0, 0]});
  // a single point to look at, so the camera has something to frame and the overlay has
  // something to project
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  pd.getPoints().setData(Float32Array.from([0, 0, 0, 1, 0, 0, 0, 1, 0]), 3);
  pd.getPolys().setData(Uint32Array.from([3, 0, 1, 2]));
  const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
  const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp);
  canvas.ren.addActor(ac);
  canvas.ren.resetCamera(); canvas.render();
  window._scene = {canvas, host, overlay: makeOverlay(canvas)};
  return true;
})()"""


def _mount(live) -> None:
    live.evaluate(_MOUNT, timeout=60)


def test_the_shared_boot_gives_one_lit_render_window(live):
    _mount(live)
    got = live.evaluate("""(() => {const s = window._scene;
      return {canvases: s.host.querySelectorAll('canvas').length,
              lights: s.canvas.ren.getLights().map(l => l.getIntensity()),
              touched: s.canvas.touched,
              disposed: s.canvas.disposed(),
              size: s.canvas.apiRW.getSize()}; })()""")
    assert got["canvases"] == 1, "a mount must leave exactly one drawing surface on its host"
    # The three-camera-light rig the geometry stage has always had, now on every scene: a key, a
    # fill and a faint rim. vtk adds a default headlight of its own at the first render, so the
    # rig is identified by its intensities rather than by a count.
    rig = sorted(round(i, 3) for i in got["lights"])
    assert [0.14, 0.28, 0.62] == rig[:3], f"expected the three-light rig, got intensities {rig}"
    assert got["touched"] is False
    assert got["disposed"] is False
    assert got["size"][0] > 0 and got["size"][1] > 0, "the render scale was never set"


def test_the_headlight_alone_is_still_available(live):
    """`lights:false` is the escape hatch: a caller that wants vtk's default keeps it."""
    _mount(live)
    lights = live.evaluate("""(async () => {
      const { mountCanvas } = await import('/static/js/viewer/scene/canvas.js');
      const h = document.createElement('div');
      h.style.cssText = 'position:relative;width:200px;height:150px';
      document.body.appendChild(h);
      const c = mountCanvas(h, {lights: false});
      const n = c.ren.getLights().map(l => l.getIntensity());
      c.dispose(); h.remove(); return n; })()""", timeout=30)
    # vtk's own headlight and nothing else: none of the rig's three intensities
    assert not ({0.62, 0.28, 0.14} & {round(i, 3) for i in lights}), lights


def test_a_press_on_the_canvas_takes_the_camera_but_a_press_on_furniture_does_not(live):
    _mount(live)
    got = live.evaluate("""(() => {const s = window._scene, out = {};
      const fire = (el) => el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
      const tool = document.createElement('button'); tool.className = 'vt';
      s.host.appendChild(tool);
      fire(tool); out.afterFurniture = s.canvas.touched;
      fire(s.host.querySelector('canvas')); out.afterCanvas = s.canvas.touched;
      return out; })()""")
    assert got["afterFurniture"] is False, "a press on an on-canvas control is not an orbit"
    assert got["afterCanvas"] is True


def test_the_resize_policy_reframes_until_the_user_takes_the_camera(live):
    _mount(live)
    got = live.evaluate("""(async () => {const s = window._scene, out = {};
      let fits = 0; s.canvas.fit = () => { fits++; };
      const settle = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(
        () => setTimeout(r, 60))));
      await settle(); fits = 0;
      s.host.style.width = '520px'; await settle();
      out.untouched = fits;
      s.canvas.touched = true; fits = 0;
      s.host.style.width = '360px'; await settle();
      out.touched = fits;
      return out; })()""", timeout=30)
    assert got["untouched"] >= 1, "a size change must re-frame while the camera is untouched"
    assert got["touched"] == 0, "once the user has the camera, a size change must not re-frame"


def test_one_loop_places_every_anchor_and_ends_with_the_last_one(live):
    _mount(live)
    got = live.evaluate("""(async () => {const s = window._scene, out = {};
      const el = document.createElement('div');
      el.style.position = 'absolute'; s.host.appendChild(el);
      const off = s.overlay.anchor(el, () => [0.3, 0.3, 0]);
      const frame = () => new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
      await frame(); await frame();
      out.running = s.overlay.running();
      out.count = s.overlay.count();
      out.left = el.style.left; out.top = el.style.top; out.shown = el.style.display;
      // a point behind the camera is hidden, not placed off the edge
      const behind = document.createElement('div'); s.host.appendChild(behind);
      const offBehind = s.overlay.anchor(behind, () => [0, 0, 1e6]);
      s.overlay.tick();
      out.behind = behind.style.display;
      off(); offBehind();
      out.afterRelease = s.overlay.count();
      return out; })()""", timeout=30)
    assert got["running"] is True, "an anchored overlay must be running a frame loop"
    assert got["count"] == 1
    assert got["shown"] == "", "an anchor in front of the camera is shown"
    assert got["left"].endswith("px") and got["top"].endswith("px")
    assert got["behind"] == "none", "a point outside the view must be hidden, not placed"
    assert got["afterRelease"] == 0


def test_the_loop_stops_when_the_canvas_is_disposed(live):
    """The leak this replaces: four loops stopped only on `!document.body.contains(host)`, and
    openViewer HIDES a previous viewer rather than removing it - so the loop ran forever."""
    _mount(live)
    got = live.evaluate("""(async () => {const s = window._scene, out = {};
      let placed = 0;
      const el = document.createElement('div'); s.host.appendChild(el);
      s.overlay.anchor(el, () => { placed++; return [0, 0, 0]; });
      const frame = () => new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
      await frame(); await frame();
      out.ranWhileLive = placed > 0;
      // the host STAYS in the document, exactly as a hidden viewer's does
      s.canvas.dispose();
      await frame(); await frame();
      const after = placed;
      await frame(); await frame();
      out.stillRunning = placed > after;
      return out; })()""", timeout=30)
    assert got["ranWhileLive"] is True
    assert got["stillRunning"] is False, (
        "the overlay kept projecting after its context was released - this is the leak")


def test_dispose_releases_the_context_and_is_idempotent(live):
    _mount(live)
    got = live.evaluate("""(() => {const s = window._scene, out = {};
      const gl = s.canvas.apiRW.get3DContext();
      out.first = s.canvas.dispose();
      out.second = s.canvas.dispose();
      out.disposed = s.canvas.disposed();
      out.canvases = s.host.querySelectorAll('canvas').length;
      out.lost = gl.isContextLost();
      // a render after dispose is a no-op rather than a throw
      let threw = false; try { s.canvas.render(); } catch (e) { threw = true; }
      out.threw = threw;
      return out; })()""")
    assert got["first"] is True and got["second"] is False, "dispose must be idempotent"
    assert got["disposed"] is True
    assert got["canvases"] == 0, "the drawing surface must be off the host after dispose"
    assert got["lost"] is True, "the WebGL context was not actually given back"
    assert got["threw"] is False
    assert_clean(live, "the scene module")


def test_dispose_is_not_wired_to_anything_yet():
    """It exists so there is something to call. Wiring it - tearing a scene down under a running
    render - is a separate change with its own evidence, so nothing may call it yet."""
    callers = []
    for path in sorted((REPO / "ui" / "js").rglob("*.js")):
        if path.name == "canvas.js":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if ".dispose()" in line:
                callers.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not callers, (
        "mountCanvas().dispose() is meant to be unwired in this step; found callers:\n  "
        + "\n  ".join(callers))
