# Responsibility: Verify the DETAIL WINDOW in a real browser - that it asks the server at all, that
# it draws every region of what comes back, and that it gets out of the way when a metric is painted.
# Boundaries: the cut is the backend's own reader packed by the route's own packer
# (detail_window.detail_response); only the network is stood in for.
"""The three defects this window shipped with, in the browser that would have caught them.

`detailWindow` was added to a file that already had a browser gate, without a test in that gate, so
on 2026-10-29 three defects shipped in it and all three were found by opening the page by hand:

    b33347f7  `tanHalf` was a const in the heatmap's closure and `radiusNow` called it from a
              sibling one, so every click threw ReferenceError before issuing any request. The
              feature had a route, a bounded reader, a face budget, a button and thirty Python
              tests, and had never once worked in a browser.
    bb46a279  a truncated window spent the whole budget on the patches it walked first, so the
              metal side of a conjugate interface was missing altogether and the payload said
              only that it had truncated.
    f4cd5ca8  painting a quality metric while the window was open coloured the merged surface,
              which is exactly the surface the window hides, so the legend described a mesh the
              reader could not see.

None of them is visible to a Python test of the route, and the first is not visible even to a check
for uncaught page errors - the handler's own try/catch wrote the ReferenceError into the button's
label and logged it with console.warn, not console.error. What catches it is that no request was
issued at all, which is what this asserts.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import assert_clean
from tests.foam_fixtures import ROW_SHEAR, write_row_of_hexes

pytestmark = pytest.mark.ui

JOB = "detail-job"
#: The middle of the row of hexes, and a radius that reaches every one of its boundary faces -
#: measured, because the shear moves the far face out of a smaller ball: at r=3 the outlet and one
#: wall face fall out of range and the counts below stop being the whole mesh.
CENTRE = (1.5, 0.5, 0.5)
REACHES_EVERYTHING = 6.0
#: What that ball holds, per region: inlet 1, outlet 1, wall 12.
FACES_PER_REGION = 14
PATCHES_PER_REGION = 3
#: A budget that cannot fit one region, let alone two, so the window has to share it out.
TRUNCATING_BUDGET = 8


def _workspace(tmp_path, *, regions: tuple[str, ...] = ()):
    """A delivered workspace on disk: three unit hexes along x, sheared at the far end.

    With `regions` it is the shape a conjugate delivery has - one polyMesh per named region AND
    the pre-split block still at the root, because `splitMeshRegions` leaves it there. The cut
    reads the named regions and refuses the root, which is what `delivered_regions` is for.
    """
    for region in regions:
        write_row_of_hexes(tmp_path / "constant" / region / "polyMesh", shear_last_y=ROW_SHEAR)
    write_row_of_hexes(tmp_path / "constant" / "polyMesh", shear_last_y=ROW_SHEAR)
    return tmp_path


def _surface(workspace) -> dict:
    """The /surface payload the backend builds for that mesh.

    The Detail button exists for a polyMesh and nothing else, and the quality fields are what make
    a metric paintable - which is the third defect's whole subject.
    """
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    surf = polymesh_viewer(workspace, roles={"wall": "wall", "inlet": "inlet",
                                             "outlet": "outlet"}, units="m")
    assert surf and surf.get("kind") == "polymesh", surf
    assert "quality_fields" in surf, surf
    surf.update(is_mesh=True, cell_count=3, parts=[],
                quality={"cells": 3, "faces": 16, "engine": "snappy", "units": "m",
                         "max_non_ortho": 68.2, "max_skewness": 0.5})
    return surf


def _cut(workspace, *, monkeypatch=None, budget: int | None = None) -> dict:
    """What the ROUTE would answer for a ball over that whole mesh, built by the route's own packer.

    `budget` stands in for `MAX_RETURN_FACES`, the cap the real route answers under: the delivered
    coupon needs a 4 mm ball and 684,660 faces in range to truncate, and three hexes need a cap of
    eight. The sharing out under test is the same code either way.
    """
    from meshpipeline.render import detail_window as D
    if budget is not None:
        monkeypatch.setattr(D, "MAX_RETURN_FACES", budget)
    cut = D.detail_at(workspace, centre=CENTRE, radius=REACHES_EVERYTHING)
    return D.detail_response(JOB, cut)


def _open(live, surface: dict, cut: dict, job: str = JOB) -> None:
    """Open the shipped viewer on that mesh, with the detail route answering that cut.

    Only the network is stood in for: the viewer module is the one the application serves, and
    every request other than these three still leaves the browser.
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
             .replace("__JOB__", job), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{job}'] && window._vdbg['{job}'].detail"
                  f" && window._vdbg['{job}:heat']", timeout=90,
                  what="the viewer to initialise with a detail window and quality fields")


def _state(live, job: str = JOB) -> dict:
    """What a reader can see of the window: the actors, their patches, the merged surface's state,
    and the words on the button."""
    return live.evaluate("""(() => {
      const d = window._vdbg['__JOB__'].detail();
      const el = document.getElementById('v-detail-__JOB__');
      return {shown: d.shown, names: d.names, hidden: d.hidden, radius: d.radius,
              painted: d.painted, metric: d.metric,
              hits: window.__detail.hits, urls: window.__detail.urls,
              label: el.querySelector('span').textContent,
              pressed: el.getAttribute('aria-pressed')};
    })()""".replace("__JOB__", job))


def _press(live, job: str = JOB) -> dict:
    """Click the button a reader clicks, and wait for its handler to come back.

    The handler is async and marks itself busy for exactly as long as it is in flight, so this
    waits on the button's own state rather than on a guess at the timing - and gives up after a
    bounded wait so a window that never asks for anything fails on the request count below
    rather than on a timeout with nothing to read.
    """
    live.evaluate("""(async () => {
      const el = document.getElementById('v-detail-__JOB__');
      const before = window.__detail.hits;
      el.click();
      for (let i = 0; i < 200; i++) {
        if (!el.dataset.busy && window.__detail.hits > before) return;
        if (!el.dataset.busy && i > 8) return;   // a toggle off asks for nothing
        await new Promise(r => setTimeout(r, 25));
      }
    })()""".replace("__JOB__", job), timeout=60)
    return _state(live, job)


def test_the_detail_button_asks_the_server_and_draws_the_cells(live, tmp_path):
    """DEFECT ONE. `radiusNow` called `tanHalf` across a closure boundary, so the click threw
    before it asked for anything, and the throw went into the button's label."""
    workspace = _workspace(tmp_path)
    cut = _cut(workspace)
    assert len(cut["patches"]) == PATCHES_PER_REGION
    assert cut["faces_returned"] == FACES_PER_REGION and cut["truncated"] is False
    _open(live, _surface(workspace), cut)

    # the hook reads the radius the button would ask for, which is the call that used to throw
    rest = _state(live)
    assert rest["radius"] > 0, rest
    assert rest["shown"] == 0 and rest["hidden"] == 0 and rest["label"] == "the cells here"

    opened = _press(live)
    # THE REQUEST ITSELF. The defect never got here: it threw while working out how big a ball to
    # ask for, and the handler's own catch turned that into a label and a console.warn.
    assert opened["hits"] == 1, opened
    query = parse_qs(urlparse(opened["urls"][0]).query)
    assert sorted(query) == ["radius", "x", "y", "z"], opened["urls"]
    assert float(query["radius"][0]) > 0, opened["urls"]

    # every patch of the cut is drawn, and the merged surface it stands in front of is turned off
    assert opened["shown"] == PATCHES_PER_REGION, opened
    assert sorted(opened["names"]) == ["inlet", "outlet", "wall"], opened
    assert opened["hidden"] > 0, opened
    assert opened["label"] == f"{FACES_PER_REGION} cells, unmerged", opened
    assert opened["pressed"] == "true", opened
    assert_clean(live, "the detail window")

    # and it is a toggle: the merged surface comes back and nothing further is asked for
    closed = _press(live)
    assert closed["shown"] == 0 and closed["hidden"] == 0, closed
    assert closed["pressed"] is None and closed["label"] == "the cells here", closed
    assert closed["hits"] == 1, closed


def test_a_truncated_cut_still_draws_every_region(live, tmp_path, monkeypatch):
    """DEFECT TWO. The budget was spent walking the patches in the order they came, so one side of
    a conjugate interface was missing from the answer and `truncated` said only that it had cut."""
    workspace = _workspace(tmp_path, regions=("fluid", "solid"))
    cut = _cut(workspace, monkeypatch=monkeypatch, budget=TRUNCATING_BUDGET)
    # the window holds both regions and the budget fits neither of them whole
    assert cut["truncated"] is True
    assert cut["faces_in_range"] == 2 * FACES_PER_REGION
    assert cut["faces_returned"] == TRUNCATING_BUDGET
    _open(live, _surface(workspace), cut)

    opened = _press(live)
    assert opened["hits"] == 1, opened
    # NEITHER SIDE OF THE INTERFACE CAN GO MISSING. A region that is not in the answer is not
    # drawn, so this is the defect as a reader met it: a conjugate mesh with one region on screen.
    assert sorted(opened["names"]) == ["fluid/inlet", "fluid/outlet", "fluid/wall",
                                       "solid/inlet", "solid/outlet", "solid/wall"], opened
    assert opened["shown"] == 2 * PATCHES_PER_REGION, opened
    assert {n.split("/")[0] for n in opened["names"]} == {"fluid", "solid"}, opened
    # and it is SAID, not implied: both numbers, so a sample is not read as the whole thing
    assert opened["label"] == (f"{TRUNCATING_BUDGET} of {2 * FACES_PER_REGION} faces - zoom in"), \
        opened
    assert_clean(live, "a truncated detail window")


def test_painting_a_metric_puts_the_merged_surface_back(live, tmp_path):
    """DEFECT THREE. The colours go on the merged surface, which this window hides while it is
    open, so the legend described a mesh with no colour on it anywhere."""
    workspace = _workspace(tmp_path)
    _open(live, _surface(workspace), _cut(workspace))

    opened = _press(live)
    assert opened["shown"] == PATCHES_PER_REGION and opened["hidden"] > 0, opened

    painted = live.evaluate("""(() => {
      const h = window._vdbg['__JOB__:heat'];
      h.set('non_ortho');
      const d = window._vdbg['__JOB__'].detail();
      const el = document.getElementById('v-detail-__JOB__');
      return {active: h.active(), legend: h.legend(), colour: h.colorOf('wall', 0),
              shown: d.shown, hidden: d.hidden, names: d.names,
              label: el.querySelector('span').textContent,
              pressed: el.getAttribute('aria-pressed')};
    })()""".replace("__JOB__", JOB))

    # the metric is on, and what it coloured is the surface the reader is now looking at
    assert painted["active"] == "non_ortho" and painted["legend"] is True, painted
    assert painted["colour"] is not None, painted
    # THE WINDOW IS GONE, which is what any camera move already did: the ball was cut where the
    # camera was, so it does not survive the reader moving on to a metric either.
    assert painted["shown"] == 0 and painted["names"] == [], painted
    assert painted["hidden"] == 0, painted
    assert painted["pressed"] is None and painted["label"] == "the cells here", painted
    assert_clean(live, "a metric painted over an open detail window")


def _coloured_workspace(tmp_path):
    """A delivered workspace with the index and checkMesh's own per-face numbers beside it.

    This is what a mesh looks like after `native._boundary_indexes` has run: the boundary written
    down once, and `checkMesh -writeFields` reduced to one value per boundary face. Without it a
    cut has geometry and no colours, which is every mesh delivered before this existed.
    """
    from meshpipeline.render import boundary_index as B
    from meshpipeline.render import boundary_quality as Q
    from tests.foam_fixtures import write_quality_fields

    ws = _workspace(tmp_path)
    pm = ws / "constant" / "polyMesh"
    B.write_index(pm)
    write_quality_fields(pm.parent, cells=[11.0, 22.0, 33.0],
                         per_face={"skewness": {"inlet": [0.11], "outlet": [0.22],
                                                "wall": [0.3] * 12}})
    Q.write_quality(pm)
    return ws


def test_a_cut_is_painted_with_the_metric_the_reader_picked(live, tmp_path):
    """THE COLOURS A CUT CAN HAVE THAT THE MAIN SURFACE CANNOT.

    Nothing in a cut is merged - one delivered face is one polygon, in the order the route packed
    it - so a per-face number is a per-polygon number and painting it is honest. On the drawn
    surface at this mesh size it is not: the mesh is reduced to fit the triangle budget, one drawn
    face stands for about sixteen delivered ones, and a colour on it was measured on none of them.
    """
    workspace = _coloured_workspace(tmp_path)
    cut = _cut(workspace)
    assert all(q.get("quality_b64") for q in cut["patches"]), "the route sent no colours"
    assert cut["patches"][0]["quality_metrics"]["skewness"]["limit"] == 20.0, (
        "a cut's skewness is a BOUNDARY face's own and checkMesh allows it 20, not the "
        "internal 4 the main surface uses")
    _open(live, _surface(workspace), cut)

    live.evaluate("window._vdbg['%s:heat'].set('non_ortho')" % JOB)
    opened = _press(live)
    assert opened["shown"] == PATCHES_PER_REGION, opened
    assert opened["painted"] == PATCHES_PER_REGION, opened
    assert opened["metric"] == "non_ortho", opened
    assert opened["label"].endswith("non-orthogonality"), opened
    assert_clean(live, "a painted detail window")

    closed = _press(live)
    assert closed["painted"] == 0 and closed["metric"] == "", closed


def test_with_no_metric_picked_a_cut_shows_the_first_it_carries_and_names_it(live, tmp_path):
    """A COLOUR NOBODY CAN REACH IS NOT A FEATURE, which is why this is the default and not the
    flat tint.

    The metric controls are built from the PAYLOAD's metrics block, and on the meshes this was
    written for there is none: `quality_fields` refuses a mesh past 40 M faces and this plate's
    coolant side alone is 235 M. So on exactly those meshes a reader has nothing to pick, and a
    cut that waited to be told which metric to draw would never draw one. It takes the first the
    cut brought instead, and the button says which, because the per-patch palette is also
    colours and a reader who cannot tell them apart reads one as the other.
    """
    workspace = _coloured_workspace(tmp_path)
    _open(live, _surface(workspace), _cut(workspace))
    opened = _press(live)
    assert opened["shown"] == PATCHES_PER_REGION, opened
    assert opened["painted"] == PATCHES_PER_REGION, opened
    assert opened["metric"] == "non_ortho", opened
    assert opened["label"] == f"{FACES_PER_REGION} cells, unmerged - non-orthogonality", opened
    assert_clean(live, "a cut painted with the metric it chose")


def test_a_mesh_with_no_measured_fields_still_cuts_and_is_not_claimed_as_painted(live, tmp_path):
    """Every mesh delivered before the fields were measured. The cut still works; it simply has
    no colours, and the button does not claim a metric it is not showing."""
    workspace = _workspace(tmp_path)          # no index, no quality
    cut = _cut(workspace)
    assert all("quality_b64" not in q for q in cut["patches"]), cut
    _open(live, _surface(workspace), cut)

    live.evaluate("window._vdbg['%s:heat'].set('non_ortho')" % JOB)
    opened = _press(live)
    assert opened["shown"] == PATCHES_PER_REGION, opened
    assert opened["painted"] == 0, "it must not claim colours it was never sent"
    assert opened["metric"] == "", opened
    assert_clean(live, "a cut of a mesh with no measured fields")
