# Responsibility: Verify in a real browser that a picked face is drawn solid in the accent from every angle (NOTE-17):
# the bore of a tube, picked, read back pixel by pixel looking into the bore, at a grazing angle and from inside it -
# every pixel the bore shows is the accent, none the grey of the part, and the accent holds (no dim maroon where the
# wall turns from the light: every picked pixel within a tight band of the accent); from the side, where the bore is hidden behind
# the wall, no accent shows through; two faces at once, and a chip's x puts back the grey of that face only.
# Boundaries: the skin is built through the worker's own code from a tube made here; the server's face (faces/at) is
# stood in for in the page, its triangles NOT the skin's (turned half a step round the bore, wound the other way), as
# a CAD file's face map and its skin are tessellated apart. It reads the screen through the DevTools screenshot.
from __future__ import annotations

import base64
import io
import json
import math
import os

import numpy as np
import pytest
from conftest import assert_clean
from test_fea_stage_mesh_as import _check, _proceed
from test_geometry_stage_open_ends import LENGTH, ORIGIN, R_IN, R_OUT, SIDES, _skin, _tube

pytestmark = pytest.mark.ui

OX, OY, OZ = ORIGIN


def _wall(r: float, flip: bool) -> dict:
    """A tube wall as the server's face map would light it (metres): the same cylinder as the skin's, but its
    triangles turned half a step round and (with `flip`) wound the other way."""
    pts, tris = [], []
    for k in range(SIDES):
        a = 2 * math.pi * (k + 0.5) / SIDES
        pts += [(OX, OY + r * math.cos(a), OZ + r * math.sin(a)), (OX + LENGTH, OY + r * math.cos(a), OZ + r * math.sin(a))]
    for k in range(SIDES):
        i, j = 2 * k, 2 * ((k + 1) % SIDES)
        tris += [(i, j + 1, i + 1), (i, j, j + 1)] if flip else [(i, i + 1, j + 1), (i, j + 1, j)]
    b64 = lambda a, t: base64.b64encode(np.asarray(a, dtype=t).ravel().tobytes()).decode("ascii")  # noqa: E731
    return {"points_b64": b64(pts, np.float32), "tris_b64": b64(tris, np.uint32), "units": "m"}


FACES = [
    {"id": "Face_3", "label": "Face 3 · round Ø80 mm", "highlight": _wall(R_IN, True),
     "sig": {"area_m2": 0.075, "centroid_m": [OX + LENGTH / 2, OY, OZ], "normal": [0, 0, 1]}},
    {"id": "Face_1", "label": "Face 1 · round Ø100 mm", "highlight": _wall(R_OUT, False),
     "sig": {"area_m2": 0.094, "centroid_m": [OX + LENGTH / 2, OY, OZ], "normal": [0, 0, 1]}},
]

E = OX + LENGTH
# camera (position, focal point, near/far): looking into the bore from past its end; nearly along it (grazing, the
# bore's far wall seen at a few degrees); from inside the bore; from the side, where the bore is behind the wall
VIEWS = {
    "into_bore": ([E + 0.25, OY, OZ + 0.12], [OX + 0.1, OY, OZ - 0.02], [0.005, 20.0]),
    "grazing": ([E + 0.45, OY + 0.004, OZ + 0.028], [OX, OY, OZ - 0.01], [0.005, 20.0]),
    "inside_bore": ([OX + 0.03, OY + 0.01, OZ + 0.004], [E, OY - 0.012, OZ - 0.006], [0.001, 2.0]),
    "side": ([OX + LENGTH / 2, OY - 0.6, OZ + 0.15], [OX + LENGTH / 2, OY, OZ], [0.005, 20.0]),
}


def _open(live, sid: str) -> None:
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__, FACES = __FACES__;
      const real = window.fetch.bind(window);
      const json = (x) => Promise.resolve(new Response(JSON.stringify(x), {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.__faceq = 0;
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/geometry/__S__/check/skin')) return json(SKIN);
        if (s.includes('/geometry/__S__/check/faces/at')) return json(FACES[window.__faceq++ % FACES.length]);
        return real(u, o);
      };
      window.__confirmed = window.__confirmed || {};
      window.__chips = await import('/static/js/shell/chips.js');
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stages = window.__stages || {};
      window.__stages['__S__'] = await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed['__S__'] = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => {}});
    })()""".replace("__SKIN__", json.dumps(_skin(_tube(R_OUT, R_IN)))).replace("__CHECK__", json.dumps(_check(None)))
                  .replace("__FACES__", json.dumps(FACES)).replace("__S__", sid), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{sid}']", timeout=90, what="the stage to initialise")


def _look(live, sid: str, view: str) -> list:
    """Puts the camera at `view`, renders, and returns the canvas samples the part shows there (CSS px):
    [x, y, what] with what 'bore' (the inner wall), 'outer' (the outer wall) or 'ring' (an end), each well inside
    its outline, or 'edge' (next to another). The truth is a ray cast against the tube's own triangles."""
    pos, foc, clip = VIEWS[view]
    return live.evaluate("""(async () => {
      const g = window._vdbg['gstage:__S__'], cam = g.cam();
      const OE = await import('/static/js/viewer/open_ends.js');
      cam.setPosition(...__POS__); cam.setFocalPoint(...__FOC__); cam.setViewUp(0, 0, 1);
      cam.setClippingRange(...__CLIP__);
      g.render();
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      const index = {tris: Float64Array.from(__TRIS__), diag: 1, holes: []};
      const canvas = document.querySelector('#gstage-__S__ canvas'), rect = canvas.getBoundingClientRect();
      const STEP = 4, cols = Math.floor(rect.width / STEP), rows = Math.floor(rect.height / STEP), grid = [];
      // the axes glyph is an SVG over the canvas that lets clicks through: its corner is not read
      const glyphs = [...document.querySelectorAll('#gstage-__S__ svg')].map(s => s.getBoundingClientRect())
        .filter(r => r.width > 0 && r.width < 200);
      const kind = (x, y) => {
        const el = document.elementFromPoint(x, y);
        if (el !== canvas) return 'cover';                   // an HTML mark or the toolbar over the canvas
        if (glyphs.some(r => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom)) return 'cover';
        const ray = g.rayAt(x, y), L = Math.hypot(...ray.dir), d = ray.dir.map(v => v / L);
        const h = OE.firstHit(index, ray.origin, d);
        if (!h) return null;
        const r = Math.hypot(h.point[1] - __OY__, h.point[2] - __OZ__);
        if (Math.abs(h.normal[0]) > 0.9) return 'ring';      // an end of the tube: not picked, ever
        return r < (__RIN__ + __ROUT__) / 2 ? 'bore' : 'outer';
      };
      for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++)
        grid.push(kind(rect.left + (i + 0.5) * STEP, rect.top + (j + 0.5) * STEP));
      const out = [];
      for (let j = 1; j < rows - 1; j++) for (let i = 1; i < cols - 1; i++) {
        const k = grid[j * cols + i];
        if (!k || k === 'cover') continue;
        // well inside the face's outline: the samples round it are the same face
        const same = [[-1, 0], [1, 0], [0, -1], [0, 1]].every(([a, b]) => grid[(j + b) * cols + i + a] === k);
        out.push([rect.left + (i + 0.5) * STEP, rect.top + (j + 0.5) * STEP, same ? k : 'edge']);
      }
      return out;
    })()""".replace("__POS__", json.dumps(pos)).replace("__FOC__", json.dumps(foc)).replace("__CLIP__", json.dumps(clip))
                         .replace("__TRIS__", json.dumps(_tube(R_OUT, R_IN).ravel().tolist()))
                         .replace("__OX__", repr(OX)).replace("__OY__", repr(OY)).replace("__OZ__", repr(OZ))
                         .replace("__LEN__", repr(LENGTH)).replace("__RIN__", repr(R_IN)).replace("__ROUT__", repr(R_OUT))
                         .replace("__S__", sid), timeout=60)


def _screen(live):
    from PIL import Image
    shot = live._send("Page.captureScreenshot", {"format": "png"})
    img = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
    return img, float(live.evaluate("window.devicePixelRatio || 1"))


def _accent(live) -> tuple:
    rgb = live.evaluate("""(async () => { const { themeRgb } = await import('/static/js/viewer/toolbar.js');
      return themeRgb('--accent', [0.886, 0.173, 0.173]); })()""")
    return tuple(int(round(255 * v)) for v in rgb)


def _is_accent(px, accent) -> bool:
    """The accent, however lit (a wall turned from the light is a dark red, 53/29/28): the accent's leading channel
    leads clearly. The part's grey, lit or dark (196/194/181, 36/35/33), has no lead."""
    i = int(np.argmax(accent))
    rest = max(v for k, v in enumerate(px) if k != i)
    return px[i] - rest >= 15 and px[i] >= 1.4 * rest


def _count(live, samples, img, dpr, accent, what: str) -> tuple[int, int]:
    """(accent, not accent) among the samples of `what`."""
    w, h = img.size
    lit = other = 0
    for x, y, k in samples:
        if k != what:
            continue
        px = img.getpixel((min(w - 1, int(x * dpr)), min(h - 1, int(y * dpr))))
        if _is_accent(px, accent):
            lit += 1
        else:
            other += 1
            if os.environ.get("FACEPICK_DEBUG") and other <= 8:
                print("not accent", what, x, y, px)
    return lit, other


def _band(accent) -> tuple:
    """THE PICKED COLOUR HOLDS: every picked pixel, lit or turned from the light, stays this close to the accent -
    its leading channel at least 70% of the accent's, the other two at most 35 above the accent's (the default
    red 226/44/44: red >= 158, green and blue <= 79). A wall turned from the light in a lit red (53/29/28) is out."""
    i = int(np.argmax(accent))
    hi = max(v for k, v in enumerate(accent) if k != i) + 35
    return i, 0.70 * accent[i], hi


def _off_band(samples, img, dpr, accent, what: str) -> list:
    """The samples of `what` whose colour is outside the picked band (empty when the colour held)."""
    i, lo, hi = _band(accent)
    w, h = img.size
    out = []
    for x, y, k in samples:
        if k != what:
            continue
        px = img.getpixel((min(w - 1, int(x * dpr)), min(h - 1, int(y * dpr))))
        if px[i] < lo or any(v > hi for kk, v in enumerate(px) if kk != i):
            out.append((x, y, px))
    return out


def _keep(img, live, sid: str, name: str) -> None:
    """With FACEPICK_SHOTS set, the stage's canvas is kept there as <name>.png (the before/after pictures)."""
    where = os.environ.get("FACEPICK_SHOTS")
    if not where:
        return
    r = live.evaluate(f"(() => {{ const b = document.querySelector('#gstage-{sid} canvas').getBoundingClientRect();"
                      " return [b.left, b.top, b.right, b.bottom]; })()")
    dpr = float(live.evaluate("window.devicePixelRatio || 1"))
    os.makedirs(where, exist_ok=True)
    img.crop(tuple(int(v * dpr) for v in r)).save(os.path.join(where, name + ".png"))


def test_a_picked_face_is_solid_from_every_angle_and_never_shows_through(live):
    sid = "facesolid"
    _open(live, sid)
    _proceed(live, sid)
    live.wait_for(f"document.getElementById('gstage-{sid}').classList.contains('iv-plain')", timeout=10,
                  what="the stage to settle")
    accent = _accent(live)
    # NOTHING PICKED: the bore is the part's grey from every angle (the check itself sees the grey)
    for view in ("into_bore", "grazing"):
        samples = _look(live, sid, view)
        img, dpr = _screen(live)
        _keep(img, live, sid, f"facepick_{view}_nothing_picked")
        lit, other = _count(live, samples, img, dpr, accent, "bore")
        assert lit == 0 and other > 50, (view, lit, other)
    # THE BORE AND THE OUTER WALL PICKED (the server's triangles are not the skin's)
    hit = {"point": [OX + LENGTH / 2, OY, OZ - R_IN], "normal": [0, 0, 1]}
    got = live.evaluate(f"""(async () => [await window._vdbg.facepick.pickAt({json.dumps(hit)}),
                                          await window._vdbg.facepick.pickAt({json.dumps(hit)})])()""")
    assert got == [{"picked": "Face_3"}, {"picked": "Face_1"}], got
    seen, off = {}, {}
    for view in ("into_bore", "grazing", "inside_bore", "side"):
        samples = _look(live, sid, view)
        img, dpr = _screen(live)
        _keep(img, live, sid, f"facepick_{view}")
        seen[view] = {k: _count(live, samples, img, dpr, accent, k) for k in ("bore", "outer", "ring")}
        off[view] = _off_band(samples, img, dpr, accent, "bore") + _off_band(samples, img, dpr, accent, "outer")
    # THE PICKED COLOUR HOLDS from every angle: no dim maroon where a wall turns from the light
    assert all(not v for v in off.values()), {k: (len(v), v[:3]) for k, v in off.items()}
    for view in ("into_bore", "grazing", "inside_bore"):
        lit, other = seen[view]["bore"]
        # every pixel of the bore is the accent: no grey stripes, no part-covered face
        assert lit > 50 and other == 0, (view, seen[view])
    lit, other = seen["into_bore"]["outer"]
    assert lit > 50 and other == 0, seen["into_bore"]
    # the tube's ends, next to both picked walls, keep their grey: the light stays on its own face
    assert seen["into_bore"]["ring"][0] == 0 and seen["into_bore"]["ring"][1] > 50, seen["into_bore"]
    assert seen["side"]["outer"][1] == 0 and seen["side"]["outer"][0] > 50, seen["side"]
    # THE OUTER WALL'S x: its grey comes back, the bore stays lit
    live.evaluate("""(() => { const chips = [...document.querySelectorAll('.box-chips[data-box="chat"] .box-chip-face')];
      chips.find(c => c.textContent.includes('Face 1')).querySelector('.box-chip-x').click(); })()""")
    samples = _look(live, sid, "into_bore")
    img, dpr = _screen(live)
    lit, other = _count(live, samples, img, dpr, accent, "bore")
    assert lit > 50 and other == 0, (lit, other)
    assert not _off_band(samples, img, dpr, accent, "bore")
    assert _count(live, samples, img, dpr, accent, "outer")[0] == 0, "the outer wall stayed lit after its x"
    # FROM THE SIDE the lit bore is behind the grey outer wall: no accent shows through it anywhere
    samples = _look(live, sid, "side")
    img, dpr = _screen(live)
    _keep(img, live, sid, "facepick_side_bore_behind_wall")
    assert all(_count(live, samples, img, dpr, accent, k)[0] == 0 for k in ("bore", "outer", "ring", "edge"))
    assert _count(live, samples, img, dpr, accent, "outer")[1] > 50
    # THE BORE'S x: nothing is lit
    live.evaluate("document.querySelector('.box-chips[data-box=\"chat\"] .box-chip-face .box-chip-x').click()")
    samples = _look(live, sid, "into_bore")
    img, dpr = _screen(live)
    assert all(_count(live, samples, img, dpr, accent, k)[0] == 0 for k in ("bore", "outer", "ring"))
    assert live.evaluate("window._vdbg.facepick.lit()") == 0
    assert_clean(live, "a picked face seen from every angle")
