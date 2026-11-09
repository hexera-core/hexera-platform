# Responsibility: Verify the STREAMING VIEW in a real browser - that one pass covers the screen
# with many tiles of real cells rather than one ball, that a tile the route truncates is split
# instead of drawn, and that whatever the budget could not reach is said out loud.
# Boundaries: the tiles are the backend's own reader packed by the route's own packer
# (detail_window.detail_response); only the network is stood in for.
"""What "show me the whole mesh" can actually mean, and the two ways of faking it.

Inspect reads one ball where a reader clicks and Follow moves that ball to the middle of the view.
Neither shows the mesh, and the drawn surface that does is merged about sixteen delivered faces to
one drawn polygon, so nothing on it is a delivered face. Streaming tiles the screen and reads real
cells into every tile.

Two things can go wrong in a way no Python test of the route can see, and both of them look right:

    A PASS THAT READS ONE BALL AND CALLS IT A SCREEN. The tiling is worked out in the browser from
    the drawn mesh's own projected cell centres; if that projection is wrong - a view axis not
    squared up, a flipped screen row, a depth sign - every tile resolves to the same point and the
    pass asks for the same ball sixteen times. On screen that is indistinguishable from Follow,
    because it IS Follow. So this asserts the tile centres are DISTINCT.

    A TRUNCATED TILE DRAWN ANYWAY. The route shares its cap out across the patches, so a truncated
    ball comes back as a sample of itself. Drawn beside whole neighbours a sample does not read as
    a dense tile - it reads as holes in the mesh, which is the exact complaint that started this
    work. So this asserts a truncating route gets SPLIT and nothing is drawn from it.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import assert_clean
from tests.foam_fixtures import ROW_SHEAR, write_row_of_hexes

pytestmark = pytest.mark.ui

JOB = "stream-job"
CENTRE = (1.5, 0.5, 0.5)
REACHES_EVERYTHING = 6.0
#: what that ball holds, per region: inlet 1, outlet 1, wall 12
FACES_PER_REGION = 14
PATCHES_PER_REGION = 3
#: a budget that cannot fit one region, so every tile the stub answers reports itself truncated
TRUNCATING_BUDGET = 8
#: the first grid the pass lays over the screen, from STREAM_N0 in viewer.js
FIRST_GRID = 4


def _workspace(tmp_path):
    write_row_of_hexes(tmp_path / "constant" / "polyMesh", shear_last_y=ROW_SHEAR)
    return tmp_path


def _surface(workspace) -> dict:
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    surf = polymesh_viewer(workspace, roles={"wall": "wall", "inlet": "inlet",
                                             "outlet": "outlet"}, units="m")
    assert surf and surf.get("kind") == "polymesh", surf
    surf.update(is_mesh=True, cell_count=3, parts=[],
                quality={"cells": 3, "faces": 16, "engine": "snappy", "units": "m",
                         "max_non_ortho": 68.2, "max_skewness": 0.5})
    return surf


def _cut(workspace, *, monkeypatch=None, budget: int | None = None) -> dict:
    from meshpipeline.render import detail_window as D
    if budget is not None:
        monkeypatch.setattr(D, "MAX_RETURN_FACES", budget)
    cut = D.detail_at(workspace, centre=CENTRE, radius=REACHES_EVERYTHING)
    return D.detail_response(JOB, cut)


def _open(live, surface: dict, cut: dict) -> None:
    """Open the shipped viewer with the detail route answering that one tile for every ball.

    The stub ignores the coordinates, which is what makes the distinctness assertion below mean
    something: every tile gets the same answer, so if the pass is asking about one point it cannot
    hide behind the payload.
    """
    live.evaluate("""(async () => {
      const SURFACE = __SURFACE__, CUT = __CUT__;
      const real = window.fetch.bind(window);
      window.__detail = {hits: 0, urls: []};
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__JOB__/detail')) {
          window.__detail.hits += 1; window.__detail.urls.push(s);
          return json(CUT);
        }
        if (s.includes('/simulation/__JOB__/surface')) return json(SURFACE);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
      document.getElementById('viewer-__JOB__').scrollIntoView();
    })()""".replace("__SURFACE__", json.dumps(surface))
             .replace("__CUT__", json.dumps(cut))
             .replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}'] && window._vdbg['{JOB}'].detail",
                  timeout=90, what="the viewer to initialise with a detail window")


def _stream(live) -> dict:
    """Turn the streaming mode on and wait for its pass, then read what a reader can see of it.

    The pass marks the inspect button busy for exactly as long as it is in flight - the same flag
    a reader's own click takes - so this waits on that rather than on a guess at the timing, and
    gives up after a bounded wait so a pass that asks for nothing fails on the counts below
    rather than on a timeout with nothing to read.
    """
    return live.evaluate("""(async () => {
      const d0 = window._vdbg['__JOB__'].detail();
      const el = document.getElementById('v-insp-arm-__JOB__');
      d0.stream(true);
      for (let i = 0; i < 400; i++) {
        if (!el.dataset.busy && window.__detail.hits > 0) break;
        if (!el.dataset.busy && i > 20) break;        // a pass that asks for nothing
        await new Promise(r => setTimeout(r, 25));
      }
      const d = window._vdbg['__JOB__'].detail();
      const btn = document.getElementById('v-insp-stream-__JOB__');
      return {shown: d.shown, names: d.names, hidden: d.hidden,
              streaming: d.streaming(), faces: d.streamFaces(), tiles: d.streamTiles(),
              read: d.streamRead(), left: d.streamLeft(), dense: d.streamDense(),
              painted: d.painted, centre: d.centre,
              hits: window.__detail.hits, urls: window.__detail.urls,
              label: document.getElementById('v-insp-read-__JOB__').textContent,
              pressed: btn.getAttribute('aria-pressed')};
    })()""".replace("__JOB__", JOB), timeout=120)


def _balls(urls: list[str]) -> list[tuple[float, float, float, float]]:
    """Every ball the pass asked for, as (x, y, z, radius)."""
    out = []
    for u in urls:
        q = parse_qs(urlparse(u).query)
        assert sorted(q) == ["radius", "x", "y", "z"], u
        out.append((float(q["x"][0]), float(q["y"][0]), float(q["z"][0]),
                    float(q["radius"][0])))
    return out


def test_one_pass_covers_the_view_with_many_distinct_tiles(live, tmp_path):
    """THE WHOLE POINT: many tiles over the screen, each about a DIFFERENT place on the mesh.

    If the projection that works out where the surface is per tile were wrong in any of the ways
    it can be - a view axis not squared up against the view direction, a flipped screen row, a
    depth sign - every tile would resolve to the same point and the pass would ask for one ball
    over and over. That is Follow with extra steps, and it looks identical on screen.
    """
    workspace = _workspace(tmp_path)
    cut = _cut(workspace)
    assert cut["faces_returned"] == FACES_PER_REGION and cut["truncated"] is False
    _open(live, _surface(workspace), cut)

    s = _stream(live)
    assert s["streaming"] is True and s["pressed"] == "true", s

    # MORE THAN ONE TILE, and no more round trips than the grid it starts from can hold
    assert 1 < s["hits"] <= FIRST_GRID * FIRST_GRID, s
    assert s["read"] == s["hits"], s

    # and each one about a different place. Three unit hexes do not fill a screen, so not every
    # tile of the grid holds mesh - what matters is that the ones that do are distinct.
    balls = _balls(s["urls"])
    assert len({(round(b[0], 6), round(b[1], 6), round(b[2], 6)) for b in balls}) == len(balls), balls
    assert all(b[3] > 0 for b in balls), balls

    # every tile drew every patch of what came back, and the faces are counted not guessed
    assert s["tiles"] == s["hits"], s
    assert s["shown"] == PATCHES_PER_REGION * s["tiles"], s
    assert sorted(set(s["names"])) == ["inlet", "outlet", "wall"], s
    assert s["faces"] == FACES_PER_REGION * s["tiles"], s
    assert s["left"] == 0, s

    # the merged surface is ghosted once for the whole pass, not once per tile
    assert s["hidden"] > 0, s

    # and the readout says what is on screen, in cells and in tiles
    assert s["label"].startswith(f"{s['faces']:,} real cells in {s['tiles']} tiles"), s["label"]
    assert "sampled" not in s["label"] and "budget" not in s["label"], s["label"]
    assert_clean(live, "the streaming view")


def test_turning_it_off_puts_the_mesh_back(live, tmp_path):
    """Streaming hides the merged surface behind dozens of actors; off has to undo all of it."""
    workspace = _workspace(tmp_path)
    _open(live, _surface(workspace), _cut(workspace))
    on = _stream(live)
    assert on["shown"] > 0 and on["hidden"] > 0, on

    off = live.evaluate("""(() => {
      const d = window._vdbg['__JOB__'].detail();
      d.stream(false);
      const e = window._vdbg['__JOB__'].detail();
      const btn = document.getElementById('v-insp-stream-__JOB__');
      return {shown: e.shown, hidden: e.hidden, streaming: e.streaming(),
              faces: e.streamFaces(), tiles: e.streamTiles(),
              label: document.getElementById('v-insp-read-__JOB__').textContent,
              pressed: btn.getAttribute('aria-pressed')};
    })()""".replace("__JOB__", JOB), timeout=60)
    assert off["streaming"] is False and off["pressed"] == "false", off
    assert off["shown"] == 0 and off["hidden"] == 0, off
    assert off["faces"] == 0 and off["tiles"] == 0, off
    assert off["label"] == "click the mesh", off


def test_a_truncated_tile_is_split_and_never_drawn(live, tmp_path, monkeypatch):
    """A SAMPLE DRAWN BESIDE WHOLE NEIGHBOURS READS AS HOLES IN THE MESH.

    That is not a hypothetical: holes punched by the drawn surface's reducer are the complaint this
    whole mode came out of, and a truncated tile would put them back - this time with the mesh
    being fine and the viewer inventing them.

    The stub reports every ball truncated, which cannot happen at every level on a real mesh but
    is exactly the worst case: the pass has to keep splitting, has to stop when its budget says
    so, and has to say that is what happened rather than reporting an empty view.
    """
    workspace = _workspace(tmp_path)
    cut = _cut(workspace, monkeypatch=monkeypatch, budget=TRUNCATING_BUDGET)
    assert cut["truncated"] is True, cut
    assert cut["faces_in_range"] > cut["faces_returned"] == TRUNCATING_BUDGET, cut
    _open(live, _surface(workspace), cut)

    s = _stream(live)
    # IT SPLIT: more tiles were read than the grid it started from holds
    assert s["read"] > FIRST_GRID * FIRST_GRID, s
    # AND NOTHING WAS DRAWN FROM A TRUNCATED ONE
    assert s["tiles"] == 0 and s["shown"] == 0 and s["faces"] == 0, s
    # the budget stopped it, and it says so in the words of what actually happened - not "no
    # cells in view", which is the opposite of too much mesh to read
    assert s["label"] == "too much mesh in view to read a tile of it whole - zoom in", s["label"]
    # and the tiles that ran out of room to split are counted. Three hexes reach the finest grid
    # before the budget bites, so these - not `left` - are what this pass could not read.
    assert s["dense"] > 0, s
    assert_clean(live, "a streaming pass that could not read a tile whole")
