# Responsibility: Verify the geometry check opens as a 3D stage in a real browser - the part with a
# pin on every opening, a pin click selecting its row, and proceeding confirming the edited form
# and handing the workbench back; that the part is on screen from the upload, before the first
# answer has settled what the mesh is for; that the preview's welded drawing is replaced in place by
# the file's own named solids when the full reading lands, from EVERY record state that says so
# (running, named on the preview) and from none that does not (kept); that an FEA assembly is drawn
# as its named solids too; that the form the user has typed on is never taken away when the route
# moves or a step fails; that a ready check with no skin still gets its card; and that a session
# opens one stage, one scene and one WebGL context.
# Boundaries: the skin payload is the one the worker stores; only the network is stood in for.
#
# MEASURED, 2026-11-12, in Chrome on this branch. Before: the stage opened only once `route` was
# set (one whole turn of chat text after the upload), and the skin was fetched exactly once, so a
# six-solid cold plate stayed one grey patch for the session. After: a part-only stage from the
# upload, 1 patch; the form and 3 named, coloured patches once the route lands or the full
# reading does; one .gc-stage in the page throughout, never an orphan beside it.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "stage-session"


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (1.0, 0.5, 0.5))}, {}, "m")
    # stl_response answers None for a surface it cannot pack; a row driven from that would fail
    # later and somewhere else, so it is caught where it is made
    assert resp is not None, "the box surface could not be packed for the stage"
    resp.update(is_mesh=False, cell_count=0)
    return resp


#: What the file's own solids look like on the wire. The three fields the renderer branches on are
#: `parts`, each patch's `color` and its `name`; the server builds the same payload with the same
#: helpers (cad/solid_parts.parts_payload, whose keys tests/unit/cad assert on against a real STEP).
PART_NAMES = ["TopLid", "MicrofluidicCore", "IOSplitter"]


def _parts_skin() -> dict:
    import numpy as np

    from meshpipeline.cad.ingest.ecxml_place import PART_COLOURS
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.skin_mesh import edges_block, prepare_skin, skin_patch

    # the same 1.0 x 0.5 x 0.5 box as _skin(), in three slabs: the bounds the camera framed on
    # do not move when the drawing is replaced, which is what makes replacing it in place safe
    slabs = [((0.0, 0.0, 0.0), (0.32, 0.5, 0.5)), ((0.34, 0.0, 0.0), (0.66, 0.5, 0.5)),
             ((0.68, 0.0, 0.0), (1.0, 0.5, 0.5))]
    patches, first = [], None
    for k, (lo, hi) in enumerate(slabs):
        tris = np.asarray(_box_triangles(lo, hi), dtype=float).reshape(-1, 3, 3)
        prepared = prepare_skin(tris)
        if first is None:
            first = prepared
        patch = skin_patch(prepared, name=PART_NAMES[k])
        patch.update(part_id=f"Part_{k + 1}", color=list(PART_COLOURS[k]), triangles=int(len(tris)))
        patches.append(patch)
    assert first is not None, "the slabs are a fixed list; the first one is always prepared"
    return {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0, "parts": True,
            "patches": patches, "part_count": len(patches), "edges": edges_block(first)}


def _check() -> dict:
    # two openings on the box's short ends, as the scout would report them: metres for the
    # scene, millimetres for the person
    return {"status": "ready", "skin": True, "pictures": [], "proposal": {
        "part": "test duct", "input_kind": "body-surface", "flow": "internal",
        "size_mm": [1000.0, 500.0, 500.0], "seed_point_mm": [500.0, 250.0, 250.0], "notes": [],
        "openings": [
            {"id": 1, "name": "inlet", "role": "inlet", "shape": "circle", "diameter_mm": 400.0,
             "centroid_m": [0.0, 0.25, 0.25], "centroid_mm": [0.0, 250.0, 250.0], "normal": [-1, 0, 0], "confidence": 0.9},
            # a name with a quote in it: it must survive the form's attribute, not end it early
            {"id": 2, "name": '2" outlet', "role": "outlet", "shape": "circle", "diameter_mm": 400.0,
             "centroid_m": [1.0, 0.25, 0.25], "centroid_mm": [1000.0, 250.0, 250.0], "normal": [1, 0, 0], "confidence": 0.8},
        ]}}


def test_the_check_opens_as_a_stage_and_proceeding_confirms_the_edited_form(live):
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/geometry/__S__/check/skin'))
          return Promise.resolve(new Response(JSON.stringify(SKIN),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        return real(u, o);
      };
      window.__confirmed = null;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(_check()))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90,
                  what="the geometry stage to initialise")

    # the stage took the workbench, with one pin per opening on the canvas
    opened = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      return {{pins: h.pins(), selected: h.selected(), fellBack: !!window.__fellBack,
               wb: document.getElementById('app').classList.contains('wb'),
               inWorkbench: !!document.querySelector('#workbench #gstage-{SESSION}'),
               rows: document.querySelectorAll('#gstage-{SESSION} .gc-table tbody tr').length,
               pinEls: document.querySelectorAll('#gstage-{SESSION} .gc-pin').length}};
    }})()""")
    assert opened["fellBack"] is False, opened
    assert opened["pins"] == 2 and opened["pinEls"] == 2 and opened["rows"] == 2, opened
    assert opened["wb"] is True and opened["inWorkbench"] is True, opened
    assert opened["selected"] is None

    # clicking pin 2 selects its row; looking into opening 1 selects that one instead
    picked = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      [...document.querySelectorAll('#gstage-{SESSION} .gc-pin')].find(p => p.textContent === '2').click();
      const afterPin = {{sel: h.selected(),
        row: document.querySelector('#gstage-{SESSION} .gc-table tr.sel')?.dataset.id || null}};
      h.look(1);
      return {{afterPin, afterLook: h.selected(), visible: h.visible()}};
    }})()""")
    assert picked["afterPin"] == {"sel": 2, "row": "2"}, picked
    assert picked["afterLook"] == 1, picked
    assert picked["visible"] >= 1

    # rename opening 1 and proceed: the confirm body carries the edit, and the page is handed back
    live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      const inp = root.querySelector('tr[data-id="1"] .gc-name'); inp.value = 'water_in';
      root.querySelector('tr[data-id="2"] .gc-role').value = 'outlet';
      root.querySelector('.gc-proceed').click();
    }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    done = live.evaluate(f"""(() => ({{
      body: window.__confirmed,
      gone: !document.getElementById('gstage-{SESSION}'),
      wb: document.getElementById('app').classList.contains('wb'),
      hook: !!(window._vdbg && window._vdbg['gstage:{SESSION}'])}}))()""")
    body = done["body"]
    assert body["input_kind"] == "body-surface" and body["flow"] == "internal"
    assert [o["name"] for o in body["openings"]] == ["water_in", '2" outlet']
    assert body["openings"][0]["diameter_mm"] == 400.0 and body["openings"][0]["centroid_mm"] == [0.0, 250.0, 250.0]
    assert body["seed_point_mm"] == [500.0, 250.0, 250.0]
    assert done["gone"] is True and done["wb"] is False and done["hook"] is False, done
    assert_clean(live, "the geometry stage")


#   -- the drawing is replaced in place when the full reading lands ------------------------------

PREVIEW_SESSION = "preview-session"


def _preview_check() -> dict:
    """The check as the stage opens on a big file: the quick drawing, still being measured."""
    return dict(_check(), status="scouted", preview="running", named=False, route="flow")


def _full_check() -> dict:
    """The same check once the full reading has landed - no `preview`, which is the client's one
    signal that the stored skin has been replaced (geometry_check._scout stores it at the same key)."""
    return dict(_check(), status="scouted", named=False, route="flow")


def test_the_named_solids_replace_the_preview_s_welded_drawing_in_place(live):
    """THE HALF THAT MAKES THE SERVER FIX VISIBLE. The stage used to fetch the skin exactly once and
    `update` never re-fetched, so a stage opened on the preview - which every big STEP does, and a
    semiconductor cold plate is a big STEP - kept the one welded grey patch for the whole session
    however many named solids the server went on to store."""
    live.evaluate("""(async () => {
      const WELDED = __WELDED__, PARTS = __PARTS__;
      window.__skin = WELDED;
      const real = window.fetch.bind(window);
      window.__skinFetches = 0;
      window.fetch = (u, o) => {
        if (String(u).includes('/geometry/__S__/check/skin')) {
          window.__skinFetches += 1;
          return Promise.resolve(new Response(JSON.stringify(window.__skin),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return real(u, o);
      };
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stage = await openGeometryStage('__S__', __PREVIEW__, async () => ({message: 'ok'}),
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack3 = true; }});
      window.__parts = PARTS;
    })()""".replace("__WELDED__", json.dumps(_skin())).replace("__PARTS__", json.dumps(_parts_skin()))
       .replace("__PREVIEW__", json.dumps(_preview_check())).replace("__S__", PREVIEW_SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{PREVIEW_SESSION}']", timeout=90,
                  what="the stage to open on the preview")

    before = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{PREVIEW_SESSION}'];
      return {{patches: h.patches(), names: h.partNames(), fetches: window.__skinFetches,
               legend: document.querySelectorAll('#gstage-{PREVIEW_SESSION} .gc-legend div').length}};
    }})()""")
    assert before["patches"] == 1 and before["legend"] == 0, before
    assert before["fetches"] == 1, before

    # the poll brings the check that no longer says "preview": the stage re-fetches the skin
    live.evaluate(f"""(() => {{
      window.__skin = window.__parts;
      window.__stage.update({json.dumps(_full_check())});
    }})()""")
    live.wait_for(f"window._vdbg['gstage:{PREVIEW_SESSION}'].patches() === 3", timeout=60,
                  what="the named solids to replace the welded drawing")
    after = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{PREVIEW_SESSION}'];
      return {{patches: h.patches(), names: h.partNames(), fetches: window.__skinFetches,
               legend: [...document.querySelectorAll('#gstage-{PREVIEW_SESSION} .gc-legend div')]
                 .map(e => e.textContent.trim()),
               stages: document.querySelectorAll('.viewer.gc-stage').length,
               pins: h.pins(), rows: document.querySelectorAll('#gstage-{PREVIEW_SESSION} .gc-table tbody tr').length}};
    }})()""")
    assert after["names"] == PART_NAMES, after
    assert after["legend"] == PART_NAMES, after             # each solid named beside its own colour
    assert after["fetches"] == 2, after                     # fetched again, once, not on every poll
    assert after["stages"] == 1, after                      # replaced in place, not stacked
    # the form and the stickers are untouched: only the drawing changed
    assert after["pins"] == 2 and after["rows"] == 2, after

    # ...and a repeat of the same poll does not fetch a third time
    live.evaluate(f"window.__stage.update({json.dumps(_full_check())})")
    live.settle()
    assert live.evaluate("window.__skinFetches") == 2
    assert_clean(live, "the stage taking the named solids")


#   -- the part is on screen from the upload, not from the first answer ---------------------------

UPLOAD_SESSION = "upload-session"


def _scouted_unrouted() -> dict:
    """What the server serves in the turn after the upload: the part is drawn and measured, and
    `route` is the empty string because the user has not yet said what the mesh is for."""
    return dict(_check(), status="scouted", preview="running", named=False, route="")


def test_the_part_is_on_screen_from_the_upload_and_the_form_follows_the_route(live):
    """THE WHOLE COMPOSER, driven as a user drives it: a file picked, then the poll. `route` is ""
    for the first turn, and the console used to open nothing at all until it was set - so a
    customer who had just uploaded their CAD saw chat text and a progress line and no part, at the
    moment of highest doubt about whether their file had been read. The part opens at once with no
    form, and the form follows when the route lands."""
    live.evaluate("""(async () => {
      window.__check = __SCOUTED__;
      window.__skin = __WELDED__;
      const real = window.fetch.bind(window);
      const j = (b) => Promise.resolve(new Response(JSON.stringify(b),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/upload/step-file')) return j({session_id: '__S__',
          step_filename: 'cold_plate.step', intake_greeting: 'What is this mesh for?'});
        if (s.includes('/geometry/__S__/check/skin')) return j(window.__skin);
        if (s.includes('/geometry/__S__/check')) return j(window.__check);
        return real(u, o);
      };
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(512)], 'cold_plate.step', {type: 'model/step'}));
      const input = document.getElementById('step-file-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    })()""".replace("__SCOUTED__", json.dumps(_scouted_unrouted()))
       .replace("__WELDED__", json.dumps(_skin())).replace("__S__", UPLOAD_SESSION), timeout=60)
    live.wait_for(f"document.querySelector('#gstage-{UPLOAD_SESSION}.gc-waiting') !== null",
                  timeout=90, what="the part to open before the first answer")
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{UPLOAD_SESSION}']", timeout=90,
                  what="the part-only scene to initialise")

    waiting = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{UPLOAD_SESSION}');
      const inp = document.getElementById('chat-input');
      return {{inWorkbench: !!document.querySelector('#workbench #gstage-{UPLOAD_SESSION}'),
               wb: document.getElementById('app').classList.contains('wb'),
               patches: window._vdbg['gstage:{UPLOAD_SESSION}'].patches(),
               proceed: root.querySelectorAll('.gc-proceed').length,
               table: root.querySelectorAll('.gc-table').length,
               pins: root.querySelectorAll('.gc-pin').length,
               lead: root.querySelector('.gc-lead').textContent.trim().slice(0, 24),
               canType: !inp.disabled}};
    }})()""")
    assert waiting["inWorkbench"] is True and waiting["wb"] is True, waiting
    assert waiting["patches"] == 1, waiting
    # NOTHING TO ANSWER YET. The form a part is answered on depends on the route - an FEA part must
    # never be shown the flow form and its stickers - so the part is shown and the form waits.
    assert waiting["proceed"] == 0 and waiting["table"] == 0 and waiting["pins"] == 0, waiting
    assert waiting["lead"].startswith("test duct"), waiting
    assert waiting["canType"] is True, "the chat is where the user answers; it must not be held"

    # the first answer settles the route, and the full reading lands with it
    live.evaluate(f"""(() => {{
      window.__skin = {json.dumps(_parts_skin())};
      window.__check = {json.dumps(dict(_check(), route="flow"))};
    }})()""")
    live.wait_for(f"document.querySelectorAll('#gstage-{UPLOAD_SESSION} .gc-proceed').length === 1",
                  timeout=90, what="the form to follow the settled route")
    routed = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{UPLOAD_SESSION}');
      const h = window._vdbg['gstage:{UPLOAD_SESSION}'];
      return {{waiting: root.classList.contains('gc-waiting'),
               stages: document.querySelectorAll('.viewer.gc-stage').length,
               hooks: Object.keys(window._vdbg).filter(k => k.startsWith('gstage:')).length,
               patches: h.patches(), names: h.partNames(), pins: h.pins(),
               rows: root.querySelectorAll('.gc-table tbody tr').length}};
    }})()""")
    assert routed["waiting"] is False, routed
    # ONE STAGE, NOT TWO. The `ready` branch used to mount a second stage and throw the handle
    # away, leaving a vtk scene nothing ever stopped under the one the user could see.
    assert routed["stages"] == 1 and routed["hooks"] == 1, routed
    assert routed["patches"] == 3 and routed["names"] == PART_NAMES, routed
    assert routed["pins"] == 2 and routed["rows"] == 2, routed
    assert_clean(live, "the part shown from the upload")


def test_a_check_without_a_skin_falls_back_to_the_card(live):
    live.evaluate("""(async () => {
      const CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__-bare/check/skin')
        ? Promise.resolve(new Response(JSON.stringify({detail: 'No skin'}),
            {status: 404, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__fellBack2 = false;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      const r = await openGeometryStage('__S__-bare', CHECK, async () => ({}),
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack2 = true; }});
      window.__stageResult = r;
    })()""".replace("__CHECK__", json.dumps(_check())).replace("__S__", SESSION), timeout=60)
    live.wait_for("window.__fellBack2 === true", timeout=60, what="the stage to fall back to the card")
    state = live.evaluate(f"""(() => ({{
      result: window.__stageResult,
      gone: !document.getElementById('gstage-{SESSION}-bare'),
      wb: document.getElementById('app').classList.contains('wb')}}))()""")
    assert state["result"] is None and state["gone"] is True and state["wb"] is False, state


#   -- the states the record can actually be in when the stage opens ------------------------------
#
# WHAT WAS MISSING. Both rows above drive `preview: "running"` only, and the record the measured
# first customer assembly was really in is a different one: the naming named the PREVIEW and
# geometry_check._land_named POPS `preview` off the record it writes, leaving status ready,
# `named_on_preview` true and no preview key. And `kept_preview` writes a THIRD state,
# `preview: "kept"`, which means the opposite - the full reading gave up and the quick drawing is
# the answer, so there is nothing to re-fetch.

NAMED_ON_PREVIEW_SESSION = "named-on-preview-session"
KEPT_SESSION = "kept-preview-session"


def _named_on_preview() -> dict:
    """What api/v1/geometry serves when the NAMING landed before the full reading: status ready,
    `named_on_preview` true, and NO `preview` key at all - _land_named pops it (geometry_check.py
    :1810). Measured on session fb5e5589: the naming named the preview at 1,022.2 s against a
    scout that needed 1,303.9 s, so this is the state a real customer's assembly was in."""
    return dict(_check(), status="ready", named=True, named_on_preview=True, route="flow")


def _named_on_full() -> dict:
    """The same session once the scout's full reading landed under the naming's answer
    (geometry_check.merged_onto_full, which drops both `preview` and `named_on_preview`)."""
    return dict(_check(), status="ready", named=True, route="flow")


def _kept_preview() -> dict:
    """The standing outcome when the full reading gives up on a big assembly and nothing was
    stored but the preview (geometry_check.kept_preview): scouted, `preview` "kept". The stored
    skin is still the welded lump, so a re-fetch would download the whole of it to draw exactly
    what is already on screen."""
    return dict(_check(), status="scouted", named=False, route="flow", preview="kept",
                preview_reason="the scout ran out of time")


def _open_stage(live, sid, check, skin):
    """openGeometryStage over a counted skin endpoint, with `window.__skin` swappable. What
    Proceed sent is kept on `window.__confirmed`: the body is the answer, and a panel that reads
    one size while it confirms another is a defect the DOM alone cannot show."""
    live.evaluate("""(async () => {
      window.__skin = __SKIN__;
      window.__skinFetches = 0;
      window.__skinFails = false;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        if (String(u).includes('/geometry/__S__/check/skin')) {
          window.__skinFetches += 1;
          if (window.__skinFails) return Promise.resolve(new Response('{"detail": "nope"}',
            {status: 500, headers: {'Content-Type': 'application/json'}}));
          return Promise.resolve(new Response(JSON.stringify(window.__skin),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return real(u, o);
      };
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__confirmed = null;
      window.__stage = await openGeometryStage('__S__', __CHECK__,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fell = true; }});
    })()""".replace("__SKIN__", json.dumps(skin)).replace("__CHECK__", json.dumps(check))
       .replace("__S__", sid), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{sid}']", timeout=90,
                  what="the stage to open")


def _drawn(live, sid) -> dict:
    return live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{sid}'];
      return {{patches: h.patches(), names: h.partNames(), fetches: window.__skinFetches,
               stages: document.querySelectorAll('.viewer.gc-stage').length,
               wb: document.getElementById('app').classList.contains('wb'),
               collapsed: document.getElementById('app').classList.contains('wb-collapsed'),
               pins: h.pins()}};
    }})()""")


def test_a_naming_that_landed_on_the_preview_still_takes_the_named_solids(live):
    """THE STATE THE REAL ASSEMBLY WAS IN, and the one the build-time latch could not survive.
    `preview` is already gone from this record, so a stage that read it once at build was disarmed
    for good: measured on the unfixed code as {"patches": 1, "names": ["skin"], "fetches": 1} after
    the merged full reading was pushed at it - one welded grey patch, never re-fetched."""
    _open_stage(live, NAMED_ON_PREVIEW_SESSION, _named_on_preview(), _skin())
    before = _drawn(live, NAMED_ON_PREVIEW_SESSION)
    assert before["patches"] == 1 and before["fetches"] == 1, before

    live.evaluate(f"""(() => {{
      window.__skin = {json.dumps(_parts_skin())};
      window.__stage.update({json.dumps(_named_on_full())});
    }})()""")
    live.wait_for(f"window._vdbg['gstage:{NAMED_ON_PREVIEW_SESSION}'].patches() === 3", timeout=60,
                  what="the named solids to replace the welded drawing")
    after = _drawn(live, NAMED_ON_PREVIEW_SESSION)
    assert after["names"] == PART_NAMES, after
    assert after["fetches"] == 2 and after["stages"] == 1, after
    assert_clean(live, "a naming made on the preview")


def _ready_over_kept(monkeypatch) -> dict:
    """THE FOURTH RECORD STATE, built by the REAL chain rather than written out here: a preview
    whose full reading gave up (`kept_preview`), stored, and then NAMED. `_land_named` pops
    `preview` off the result it writes and `write_status` stores that result on its own, merging
    no old record back (geometry_check.py:148, :1827) - so what is served is `ready` with no
    `preview` key and no `named_on_preview`, while the stored skin is still the preview's welded
    lump, because the full reading never finished and never will. `preview_reason` is the one key
    that survives, and only this lineage carries it: `merged_onto_full` takes its keys from the
    FULL reading (:1859), which has none."""
    from meshpipeline.application import geometry_check as gc

    store: dict[str, dict] = {}
    monkeypatch.setattr(gc, "_store_json", lambda key, payload: store.__setitem__(
        key, json.loads(json.dumps(gc.jsonable(payload)))))
    monkeypatch.setattr(gc, "read_check", lambda sid: store.get(gc.check_object_key(sid, "scout.json")))
    sid, key = KEPT_SESSION, gc.check_object_key(KEPT_SESSION, "scout.json")
    preview = {"named": False, "skin": True, "pictures": [], "skin_key": f"{sid}/skin.json",
               "facts": {"scale_to_m": 0.001, "notes": [], "validity": {"kind": "unchecked"}},
               "proposal": dict(_check()["proposal"]), "preview": gc.PREVIEW_RUNNING}
    gc.write_status(sid, gc.STATUS_SCOUTED, **preview)
    kept = gc.kept_preview({**store[key], "status": gc.STATUS_SCOUTED}, "the scout ran out of time")
    gc.store_status(sid, kept.pop("status"), gc.RETRY_SCOUT,
                    **{k: v for k, v in kept.items() if k not in gc._STAMPED_KEYS})
    stored_kept = dict(store[key])
    # the naming then runs over it, building its result exactly as run_geometry_naming does
    result = {k: v for k, v in stored_kept.items() if k not in gc._STAMPED_KEYS + gc._FAILURE_KEYS}
    result.update(named=True, vision={"answer": "ok"}, purpose_text="cool this plate",
                  naming_seconds=12.3)
    gc._land_named(sid, result, stored_kept)
    ready = dict(store[key])
    # the halves of the finding, measured on the record itself rather than assumed
    assert ready["status"] == "ready", ready
    assert ready.get("preview") is None and ready.get("named_on_preview") is None, sorted(ready)
    assert ready.get("preview_reason") == "the scout ran out of time", sorted(ready)
    return dict(_check(), status="ready", named=True, route="flow",
                preview_reason=ready["preview_reason"])


def test_a_kept_preview_is_not_re_fetched(live, monkeypatch):
    """`preview: "kept"` says the full reading GAVE UP and the quick drawing is the answer. The
    unfixed disarm test was `d2.preview === "running"`, so this fired it: on the unfixed code the
    fetch count goes 1 -> 2 and the whole scene is rebuilt to draw what is already on screen, on
    the 15.9 MB-class assemblies this path exists for, throwing the user's clicks away.

    AND THE STATE AFTER IT, which reading the ABSENCE of `preview` as the full reading missed
    outright: the naming lands on the kept preview and the record stored over it is `ready` with
    no preview key at all. Measured in Chrome on the unfixed tree, the three states in order: the
    fetch count went 1 -> 1 -> 2 and the scene was rebuilt to draw the identical lump."""
    _open_stage(live, KEPT_SESSION, _preview_check(), _skin())
    assert _drawn(live, KEPT_SESSION)["fetches"] == 1

    # the full reading gave up; the quick drawing stands
    live.evaluate(f"""(() => {{
      window.__skin = {json.dumps(_parts_skin())};     // what a re-fetch would wrongly bring in
      window.__stage.update({json.dumps(_kept_preview())});
    }})()""")
    live.settle()
    kept = _drawn(live, KEPT_SESSION)
    assert kept["fetches"] == 1, kept
    assert kept["patches"] == 1 and kept["pins"] == 2, kept
    # ...and a kept preview the stage OPENED on is not re-fetched either
    live.evaluate(f"window.__stage.update({json.dumps(_kept_preview())})")
    live.settle()
    assert live.evaluate("window.__skinFetches") == 1

    # ...and neither is the READY record the naming writes over it, which carries neither
    # `preview` nor `named_on_preview` and is served on top of the same welded lump
    live.evaluate(f"window.__stage.update({json.dumps(_ready_over_kept(monkeypatch))})")
    live.settle()
    after = _drawn(live, KEPT_SESSION)
    assert after["fetches"] == 1, after
    assert after["patches"] == 1 and after["names"] == ["skin"] and after["pins"] == 2, after
    assert_clean(live, "a kept preview")


def test_a_skin_re_fetch_that_fails_leaves_the_stage_and_the_workbench(live):
    """THE DRAWING IS NEVER THE USER'S WAY ON. A transient failure on the re-fetch leaves the
    preview's drawing, which is the part, correctly drawn in one piece - and above all leaves the
    stage in the workbench. The replace-the-stage version of this could strip `wb` off #app on one
    transient 500 and hand the user a collapsed workbench with no stage in it."""
    _open_stage(live, "refetch-fails-session", _preview_check(), _skin())
    live.evaluate(f"""(() => {{
      window.__skinFails = true;
      window.__stage.update({json.dumps(_full_check())});
    }})()""")
    live.settle()
    state = _drawn(live, "refetch-fails-session")
    assert state["fetches"] == 2, state                    # it tried
    assert state["patches"] == 1 and state["pins"] == 2, state
    assert state["stages"] == 1 and state["wb"] is True and state["collapsed"] is False, state


#   -- an FEA part's assembly is drawn as its solids too ------------------------------------------

FEA_SESSION = "fea-parts-session"


def _fea_check(**over) -> dict:
    """An FEA part's check, the shape api/v1/geometry serves it in (fea_check.fea_view)."""
    d = {"status": "scouted", "named": False, "skin": True, "pictures": [], "route": "fea",
         "proposal": {"part": "bracket", "input_kind": "body-surface", "size_mm": [1000.0, 500.0, 500.0],
                      "unit": "mm", "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []},
         "fea": {"question": "Is this your part and are the units right?", "bodies": 3,
                 "clean": True, "clean_summary": "watertight", "single_part_only": "",
                 "can_proceed": True}}
    d.update(over)
    return d


def test_an_fea_assembly_is_drawn_as_its_named_solids(live):
    """THE FEA FOLLOWER HAD NO ROW. An FEA assembly opens on the preview like any other big file,
    and this is the first time it is drawn as coloured named solids at all - the whole reason an
    FEA customer uploading a three-body bracket can see we found three bodies and not one."""
    _open_stage(live, FEA_SESSION, _fea_check(preview="running"), _skin())
    before = live.evaluate(f"""(() => ({{
      patches: window._vdbg['gstage:{FEA_SESSION}'].patches(),
      fetches: window.__skinFetches,
      fea: !!document.querySelector('#gstage-{FEA_SESSION}.fea-stage .fea-form'),
      proceed: document.querySelectorAll('#gstage-{FEA_SESSION} .gc-proceed').length,
      pins: window._vdbg['gstage:{FEA_SESSION}'].pins()}}))()""")
    assert before == {"patches": 1, "fetches": 1, "fea": True, "proceed": 1, "pins": 0}, before

    live.evaluate(f"""(() => {{
      window.__skin = {json.dumps(_parts_skin())};
      window.__stage.update({json.dumps(_fea_check(status="ready", named=True))});
    }})()""")
    live.wait_for(f"window._vdbg['gstage:{FEA_SESSION}'].patches() === 3", timeout=60,
                  what="the FEA part's own solids to be drawn")
    after = live.evaluate(f"""(() => ({{
      names: window._vdbg['gstage:{FEA_SESSION}'].partNames(),
      fetches: window.__skinFetches,
      stages: document.querySelectorAll('.viewer.gc-stage').length,
      legend: [...document.querySelectorAll('#gstage-{FEA_SESSION} .gc-legend div')].map(e => e.textContent.trim()),
      pins: window._vdbg['gstage:{FEA_SESSION}'].pins()}}))()""")
    assert after["names"] == PART_NAMES and after["legend"] == PART_NAMES, after
    assert after["fetches"] == 2 and after["stages"] == 1, after
    assert after["pins"] == 0, "an FEA part has no openings to sticker"
    assert_clean(live, "an FEA assembly's solids")


#   -- the route landing never takes the form away ------------------------------------------------

EDIT_SESSION = "route-flip-session"


def test_what_the_user_typed_survives_the_route_landing_and_moving(live):
    """THE WALK-THROUGH THE REVIEW EXECUTED, run as a user runs it. An ordinary CFD customer says
    "mesh this for the airflow", the flow form opens, they rename opening 2 and set its role;
    naming is slow so the status stays `scouted` and the poll is live; then they send "and I want
    the stress in the bracket too". The server's route is a pure function of ALL the user's
    messages (purpose_map.reading_for joins every user text), so it flips to "fea" - and the
    version that re-mounted the stage on a route change released the flow stage under them and
    mounted the FEA stage, with their typed name and role in the stage it threw away.

    ONE STAGE: the form is built once, where the waiting note stood, and nothing moves it after."""
    live.evaluate("""(async () => {
      window.__check = __SCOUTED__;
      window.__skin = __WELDED__;
      const real = window.fetch.bind(window);
      const j = (b) => Promise.resolve(new Response(JSON.stringify(b),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.__checkPolls = 0;
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/upload/step-file')) return j({session_id: '__S__',
          step_filename: 'duct.step', intake_greeting: 'What is this mesh for?'});
        if (s.includes('/geometry/__S__/check/skin')) return j(window.__skin);
        if (s.includes('/geometry/__S__/check')) { window.__checkPolls += 1; return j(window.__check); }
        return real(u, o);
      };
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(512)], 'duct.step', {type: 'model/step'}));
      const input = document.getElementById('step-file-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    })()""".replace("__SCOUTED__", json.dumps(_scouted_unrouted()))
       .replace("__WELDED__", json.dumps(_skin())).replace("__S__", EDIT_SESSION), timeout=60)
    live.wait_for(f"document.querySelector('#gstage-{EDIT_SESSION}.gc-waiting') !== null",
                  timeout=90, what="the part to open before the first answer")

    # "mesh this for the airflow": the route lands as flow and the form is built in place
    live.evaluate(f"""(() => {{ window.__check = {json.dumps(dict(_check(), status="scouted", named=False, route="flow"))}; }})()""")
    live.wait_for(f"document.querySelectorAll('#gstage-{EDIT_SESSION} .gc-table tbody tr').length === 2",
                  timeout=90, what="the flow form to follow the settled route")
    opened = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{EDIT_SESSION}');
      root.querySelector('tr[data-id="2"] .gc-name').value = 'coolant_out';
      root.querySelector('tr[data-id="2"] .gc-role').value = 'outlet';
      return {{stages: document.querySelectorAll('.viewer.gc-stage').length,
               waiting: root.classList.contains('gc-waiting'),
               hooks: Object.keys(window._vdbg).filter(k => k.startsWith('gstage:')).length}};
    }})()""")
    assert opened == {"stages": 1, "waiting": False, "hooks": 1}, opened

    # "and I want the stress in the bracket too": the server re-reads every message and the route
    # moves to fea, while the naming is still running
    polled = live.evaluate("window.__checkPolls")
    live.evaluate(f"""(() => {{ window.__check = {json.dumps(_fea_check(preview="running"))}; }})()""")
    # two more polls, so one of them certainly carried the fea payload all the way through update()
    live.wait_for(f"window.__checkPolls >= {polled + 2}", timeout=60,
                  what="the poll to carry the moved route")
    kept = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{EDIT_SESSION}');
      return {{stages: document.querySelectorAll('.viewer.gc-stage').length,
               hooks: Object.keys(window._vdbg).filter(k => k.startsWith('gstage:')).length,
               name: root.querySelector('tr[data-id="2"] .gc-name')?.value ?? null,
               role: root.querySelector('tr[data-id="2"] .gc-role')?.value ?? null,
               rows: root.querySelectorAll('.gc-table tbody tr').length,
               wb: document.getElementById('app').classList.contains('wb')}};
    }})()""")
    assert kept["name"] == "coolant_out" and kept["role"] == "outlet", kept
    assert kept["stages"] == 1 and kept["hooks"] == 1 and kept["rows"] == 2, kept
    assert kept["wb"] is True, kept
    assert_clean(live, "a route that moved under an answered form")


#   -- the composer's own no-skin path, at ready -------------------------------------------------

CARD_SESSION = "no-skin-session"


def test_the_composer_draws_the_card_for_a_ready_check_with_no_skin(live):
    """THE COMPOSER'S OWN PATH, which calling openGeometryStage directly does not cover. A check
    that reaches `ready` with `skin` false has no 3D stage to open, and the card is drawn by the
    stage's own fallback (main.js opts.fallback) - never by deps.geometryCard, which is
    Stage.geometryCheckUpdate and returns at once while `_gcEl` is null (render/stage.js:324). The
    rewrite's `else if (stageFailed) deps.geometryCard(d)` in the ready branch, over a mount that
    returned early on `!d.skin`, left no stage AND no card where the old line left the card."""
    live.evaluate("""(async () => {
      const real = window.fetch.bind(window);
      const j = (b, s) => Promise.resolve(new Response(JSON.stringify(b),
        {status: s || 200, headers: {'Content-Type': 'application/json'}}));
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/upload/step-file')) return j({session_id: '__S__',
          step_filename: 'duct.step', intake_greeting: 'What is this mesh for?'});
        if (s.includes('/geometry/__S__/check/skin')) return j({detail: 'No skin'}, 404);
        if (s.includes('/geometry/__S__/check')) return j(__READY__);
        return real(u, o);
      };
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(512)], 'duct.step', {type: 'model/step'}));
      const input = document.getElementById('step-file-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    })()""".replace("__READY__", json.dumps(dict(_check(), status="ready", named=True,
                                                 skin=False, route="flow")))
       .replace("__S__", CARD_SESSION), timeout=60)
    live.wait_for("document.querySelectorAll('.gc-card .gc-table tbody tr').length === 2",
                  timeout=90, what="the card to stand in for the stage")
    state = live.evaluate("""(() => ({
      cards: document.querySelectorAll('.gc-card').length,
      stages: document.querySelectorAll('.viewer.gc-stage').length,
      wb: document.getElementById('app').classList.contains('wb'),
      proceed: document.querySelectorAll('.gc-card .gc-proceed').length}))()""")
    assert state == {"cards": 1, "stages": 0, "wb": False, "proceed": 1}, state


#   -- ...and at the SCOUTED poll the route lands on, which is where the customer is ------------

SCOUTED_CARD_SESSION = "no-webgl-scouted-session"


def _cannot_draw(live, sid, first):
    """The whole composer, driven from a real upload, with the skin GET answering 500 - a part
    whose stage cannot be drawn, which is also what no WebGL looks like from here.

    `window.__skinOk` is how a row says the failure was TRANSIENT: set it to a skin payload and
    the next GET serves that instead of the 500. It is null here, so every row that does not set
    it drives exactly the permanent failure it drove before."""
    live.evaluate("""(async () => {
      window.__check = __FIRST__;
      window.__polls = 0; window.__skinFetches = 0; window.__confirmed = null;
      window.__skinOk = null;
      const real = window.fetch.bind(window);
      const j = (b, s) => Promise.resolve(new Response(JSON.stringify(b),
        {status: s || 200, headers: {'Content-Type': 'application/json'}}));
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/upload/step-file')) return j({session_id: '__S__',
          step_filename: 'duct.step', intake_greeting: 'What is this mesh for?'});
        if (s.includes('/geometry/__S__/check/skin')) { window.__skinFetches += 1;
          return window.__skinOk ? j(window.__skinOk)
            : j({detail: 'the skin could not be read'}, 500); }
        if (s.includes('/geometry/__S__/check/confirm')) { window.__confirmed = JSON.parse(o.body);
          return j({message: 'noted'}); }
        if (s.includes('/geometry/__S__/check')) { window.__polls += 1; return j(window.__check); }
        return real(u, o);
      };
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(512)], 'duct.step', {type: 'model/step'}));
      const input = document.getElementById('step-file-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    })()""".replace("__FIRST__", json.dumps(first)).replace("__S__", sid), timeout=60)
    live.wait_for("window.__skinFetches >= 1", timeout=90,
                  what="the composer's first attempt at the part")


def _cards(live) -> dict:
    return live.evaluate("""(() => {
      const card = document.querySelector('.gc-card, .fea-card');
      const btn = card && card.querySelector('.gc-proceed');
      return {cards: document.querySelectorAll('.gc-card, .fea-card').length,
              stages: document.querySelectorAll('.viewer.gc-stage').length,
              rows: card ? card.querySelectorAll('.gc-table tbody tr').length : 0,
              names: card ? [...card.querySelectorAll('.gc-name')].map(i => i.value) : [],
              proceed: btn ? 1 : 0, pressable: !!(btn && !btn.disabled),
              size: card && card.querySelector('.fea-size')
                ? card.querySelector('.fea-size').textContent.replace(/\\s+/g, ' ').trim() : null,
              polls: window.__polls}; })()""")


def test_the_card_stands_in_from_the_scouted_poll_the_route_lands_on(live):
    """THE WHOLE SCOUTED PHASE WITH NOTHING ON THE PAGE. The part is shown from the UPLOAD now, so
    the composer's first mount attempt is made at the upload poll, where `route` is "" - and the
    stage draws no card there on purpose: WHICH form the card would carry is exactly what is
    unsettled, and a flow card on an FEA part is the wrongness that gate exists to prevent. That
    attempt latched `stageFailed`, every later scouted poll returned on it, and the
    `deps.geometryCard(d)` beside the call is Stage.geometryCheckUpdate, which returns at once
    while `_gcEl` is null (render/stage.js:324). So an ordinary CFD customer with no WebGL, or one
    transient 500 on the skin GET, was given NOTHING for the whole naming step - 1,022 s on the
    measured first customer assembly.

    MEASURED in Chrome on the unfixed tree, the composer driven from a real upload: cards 0 after
    the upload poll, 0 after the poll the route landed on, 0 three polls later, 1 only at ready.
    HEAD mounted only once the route was in, so HEAD had the card with its two openings and a
    working Proceed at the first poll after the user's answer. This is that again."""
    _cannot_draw(live, SCOUTED_CARD_SESSION, _scouted_unrouted())
    live.settle()
    # NOTHING YET, and that is the kept half of the gate: the route is still ""
    waiting = _cards(live)
    assert waiting["cards"] == 0 and waiting["stages"] == 0, waiting

    # "mesh this for the airflow": the route lands, the check is still scouted
    live.evaluate(f"""(() => {{ window.__check = {json.dumps(dict(_check(), status="scouted",
                                                                  named=False, route="flow"))}; }})()""")
    live.wait_for("document.querySelectorAll('.gc-card .gc-table tbody tr').length === 2",
                  timeout=90, what="the card to stand in at the scouted poll")
    card = _cards(live)
    assert card["cards"] == 1 and card["stages"] == 0, card
    assert card["rows"] == 2 and card["names"] == ["inlet", '2" outlet'], card
    assert card["proceed"] == 1 and card["pressable"] is True, card

    # and it is a working Proceed, not a drawn one: the answer reaches the server
    live.evaluate("document.querySelector('.gc-card .gc-proceed').click()")
    live.wait_for("window.__confirmed !== null", timeout=60, what="the answer to be confirmed")
    sent = live.evaluate("window.__confirmed")
    assert [o["name"] for o in sent["openings"]] == ["inlet", '2" outlet'], sent
    assert sent["size_mm"] == [1000.0, 500.0, 500.0], sent
    assert_clean(live, "the card standing in through the scouted phase")


FEA_CARD_SESSION = "no-webgl-fea-session"


def test_the_card_that_stands_in_is_the_one_the_route_asked_for(live):
    """THE OTHER HALF OF THE SAME GATE. The route that lands decides WHICH card: an FEA part gets
    the FEA panel - its size, unit, bodies and clean check - and never the flow form with its
    opening rows and stickers. Suppressing the card until the route is in is what buys this, and
    it is worth nothing if the card never arrives afterwards."""
    _cannot_draw(live, FEA_CARD_SESSION, _scouted_unrouted())
    live.settle()
    assert _cards(live)["cards"] == 0

    live.evaluate(f"""(() => {{ window.__check = {json.dumps(_fea_check())}; }})()""")
    live.wait_for("document.querySelectorAll('.fea-card .fea-size').length === 1",
                  timeout=90, what="the FEA card to stand in at the scouted poll")
    card = _cards(live)
    assert card["cards"] == 1 and card["stages"] == 0, card
    assert card["rows"] == 0, "an FEA part has no opening rows to answer"
    assert card["size"].startswith("1000"), card
    assert card["proceed"] == 1 and card["pressable"] is True, card
    assert_clean(live, "the FEA card standing in through the scouted phase")


RECOVER_SESSION = "transient-500-session"


def test_a_stage_that_could_not_draw_is_tried_again_once_the_check_is_ready(live):
    """THE OTHER HALF OF THE LATCH, and why it is BYPASSED at `ready` rather than dropped. A latch
    that never released would re-fetch a 15.9 MB-class skin on every poll for a customer with no
    WebGL, who will never have a stage; a latch with no release holds a customer whose failure has
    PASSED on the card for the rest of the session. So exactly two things buy one more attempt:
    the route landing (the row above), and the check reaching `ready` - which is what gives a card
    its pictures, and is also where one transient 500 on the skin GET has had time to clear.

    The `ready` half is what `atReady` is for in composer.mountStage, and nothing reached it: the
    row above latches on an UNROUTED failure and is released by the route, and the no-skin row
    never sets `stageFailed` at all, because the mount returns early on `!d.skin`. Removing the
    bypass left both of them green."""
    _cannot_draw(live, RECOVER_SESSION, _scouted_unrouted())
    live.settle()
    assert _cards(live)["cards"] == 0

    # the route lands and the part still cannot be drawn, so the card stands in
    live.evaluate(f"""(() => {{ window.__check = {json.dumps(dict(_check(), status="scouted",
                                                                  named=False, route="flow"))}; }})()""")
    live.wait_for("document.querySelectorAll('.gc-card .gc-table tbody tr').length === 2",
                  timeout=90, what="the card to stand in at the scouted poll")
    assert _cards(live)["stages"] == 0

    # ...the 500 was transient, and the check reaches ready
    live.evaluate(f"""(() => {{
      window.__skinOk = {json.dumps(_skin())};
      window.__check = {json.dumps(dict(_check(), status="ready", named=True, route="flow"))};
    }})()""")
    live.wait_for("document.querySelectorAll('.viewer.gc-stage').length === 1", timeout=90,
                  what="the stage to be tried again once the check is ready")
    live.settle()
    got = live.evaluate("""(() => {
      const root = document.querySelector('.viewer.gc-stage');
      return {stages: document.querySelectorAll('.viewer.gc-stage').length,
              cards: document.querySelectorAll('.gc-card, .fea-card').length,
              hooks: Object.keys(window._vdbg || {}).filter(k => k.startsWith('gstage:')).length,
              rows: root.querySelectorAll('.gc-table tbody tr').length,
              proceed: root.querySelectorAll('.gc-proceed').length}; })()""")
    # the part itself, with its form - ONE stage and ONE scene, as every other way in gives
    assert got["stages"] == 1 and got["hooks"] == 1, got
    assert got["rows"] == 2 and got["proceed"] == 1, got
    assert_clean(live, "a transient failure that had cleared by ready")


#   -- a stage is never torn down under the user, and never leaks its context -------------------

FAILED_SESSION = "failed-under-the-form-session"
SCENE_SESSION = "one-scene-session"


def test_a_failed_naming_does_not_tear_the_form_down_under_the_user(live):
    """TWO DISAGREEING NOTIONS OF AN UNSET ROUTE, measured. `openGeometryStage` branches on a
    route that is strictly "" while the composer's own routeOf() mapped a MISSING route key to ""
    as well - so this payload, which HAS a form on screen, was tracked as having none, and the
    `failed` poll below ran stage.release() on it. The stage now answers the question itself
    (hasForm), so there is only one notion of it.

    The payload with no `route` key is the one every caller of openGeometryStage has always passed
    and the one the card tests use; the server always sets the key (api/v1/geometry.py:291)."""
    live.evaluate("""(async () => {
      window.__check = __SCOUTED__;
      const real = window.fetch.bind(window);
      const j = (b) => Promise.resolve(new Response(JSON.stringify(b),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/upload/step-file')) return j({session_id: '__S__',
          step_filename: 'duct.step', intake_greeting: 'What is this mesh for?'});
        if (s.includes('/geometry/__S__/check/skin')) return j(__WELDED__);
        if (s.includes('/geometry/__S__/check')) return j(window.__check);
        return real(u, o);
      };
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(512)], 'duct.step', {type: 'model/step'}));
      const input = document.getElementById('step-file-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    })()""".replace("__SCOUTED__", json.dumps(dict(_check(), status="scouted", named=False)))
       .replace("__WELDED__", json.dumps(_skin())).replace("__S__", FAILED_SESSION), timeout=60)
    live.wait_for(f"document.querySelectorAll('#gstage-{FAILED_SESSION} .gc-table tbody tr').length === 2",
                  timeout=90, what="the stage to open with its form")
    live.evaluate(f"""(() => {{ window.__check = {json.dumps(dict(_check(), status='failed', named=False,
                                                                  reason='the naming worker died'))}; }})()""")
    live.wait_for(f"""document.querySelector('#gstage-{FAILED_SESSION} .gc-banner-text')
                      ?.textContent.includes('gave up')""",
                  timeout=90, what="the naming's failure to reach the banner")
    kept = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{FAILED_SESSION}');
      return {{stages: document.querySelectorAll('.viewer.gc-stage').length,
               rows: root ? root.querySelectorAll('.gc-table tbody tr').length : 0,
               proceed: root ? root.querySelectorAll('.gc-proceed').length : 0,
               wb: document.getElementById('app').classList.contains('wb'),
               collapsed: document.getElementById('app').classList.contains('wb-collapsed')}};
    }})()""")
    assert kept == {"stages": 1, "rows": 2, "proceed": 1, "wb": True, "collapsed": False}, kept


def test_one_session_opens_one_scene_and_one_webgl_context(live):
    """A BROWSER KEEPS ABOUT SIXTEEN WEBGL CONTEXTS and this branch has already lost one on this
    very stage (setEdgeVisibility at 1.9M faces), so a second scene per session is not free. The
    version that re-built the stage when the route landed opened two on the ordinary path, where
    there had always been one.

    MEASURED by wrapping getContext in the page - `live` re-opens the page per test, so the wrap
    goes with it - and counting the contexts handed out across the whole upload-then-answer path:
    1 with one stage, 2 with the rebuild. Nothing hands a context BACK: measured after both
    vtk.js deletes on a released stage's canvas, isContextLost() is still false, so the count is
    held down by opening one scene and not by tearing one down."""
    live.evaluate("""(async () => {
      window.__check = __SCOUTED__;
      window.__skin = __WELDED__;
      window.__ctxs = 0;
      const realGet = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (...a) {
        const c = realGet.apply(this, a);
        if (c && typeof c.isContextLost === 'function') window.__ctxs += 1;
        return c;
      };
      const real = window.fetch.bind(window);
      const j = (b) => Promise.resolve(new Response(JSON.stringify(b),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/upload/step-file')) return j({session_id: '__S__',
          step_filename: 'duct.step', intake_greeting: 'What is this mesh for?'});
        if (s.includes('/geometry/__S__/check/skin')) return j(window.__skin);
        if (s.includes('/geometry/__S__/check')) return j(window.__check);
        return real(u, o);
      };
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(512)], 'duct.step', {type: 'model/step'}));
      const input = document.getElementById('step-file-input');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    })()""".replace("__SCOUTED__", json.dumps(_scouted_unrouted()))
       .replace("__WELDED__", json.dumps(_skin())).replace("__S__", SCENE_SESSION), timeout=60)
    # the scene itself, not just the box: vtk.js is fetched and the skin with it, so the canvas -
    # and the context - arrive a moment after the stage is on the page
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SCENE_SESSION}']", timeout=90,
                  what="the part-only scene to initialise")
    assert live.evaluate("window.__ctxs") == 1

    live.evaluate(f"""(() => {{ window.__check = {json.dumps(dict(_check(), status="scouted", named=False, route="flow"))}; }})()""")
    live.wait_for(f"document.querySelectorAll('#gstage-{SCENE_SESSION} .gc-table tbody tr').length === 2",
                  timeout=90, what="the form to follow the settled route")
    live.settle()
    got = live.evaluate(f"""(() => ({{ctxs: window.__ctxs,
      canvases: document.querySelectorAll('#gstage-{SCENE_SESSION} canvas').length,
      stages: document.querySelectorAll('.viewer.gc-stage').length,
      hooks: Object.keys(window._vdbg).filter(k => k.startsWith('gstage:')).length}}))()""")
    assert got == {"ctxs": 1, "canvases": 1, "stages": 1, "hooks": 1}, got


#   -- the form is built from the payload that CARRIED the route --------------------------------

GROW_SESSION = "grow-from-the-routed-payload-session"


def test_the_fea_panel_reads_and_confirms_the_routed_payload_s_size(live):
    """THE PANEL'S OWN QUESTION IS "Is this your part and are the units right?", and it was built
    from the proposal of the PREVIOUS, unrouted poll: `p`, which update() last assigned. The FEA
    follower moves the unit, the bodies and the clean check but not the size, so the stale size
    stood for the session - and readFeaForm sends `p.size_mm` and `p.scale_to_m` on Proceed
    (render/fea_form.js:268), so the wrong number is not only read, it is answered.

    MEASURED in Chrome on the unfixed tree: a stage opened on a preview proposal of 1000x500x500,
    then one update carrying route fea AND a proposal of 2000x900x900, read "1000 x 500 x 500 mm"
    and confirmed size_mm [1000, 500, 500] - beside a bodies line correctly reading 3, which the
    follower does move. HEAD's openFeaStage copied the proposal from the routed payload."""
    _open_stage(live, GROW_SESSION, _scouted_unrouted(), _skin())
    assert live.evaluate(f"document.querySelector('#gstage-{GROW_SESSION}.gc-waiting') !== null")

    grown = _fea_check(status="scouted", named=False)
    grown["proposal"] = dict(grown["proposal"], part="bracket", size_mm=[2000.0, 900.0, 900.0],
                             scale_to_m=0.001)
    grown["fea"] = dict(grown["fea"], bodies=3)
    live.evaluate(f"window.__stage.update({json.dumps(grown)})")
    live.wait_for(f"document.querySelectorAll('#gstage-{GROW_SESSION} .fea-size').length === 1",
                  timeout=60, what="the FEA panel to be built where the waiting note stood")
    panel = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{GROW_SESSION}');
      const t = (s) => {{ const el = root.querySelector(s);
        return el ? el.textContent.replace(/\\s+/g, ' ').trim() : null; }};
      return {{size: t('.fea-size'), bodies: t('.fea-bodies'), lead: t('.gc-lead'),
               rows: root.querySelectorAll('.gc-table tbody tr').length}}; }})()""")
    assert panel["size"].startswith("2000"), panel
    assert "900" in panel["size"], panel
    assert panel["bodies"].startswith("3"), panel          # the line the follower does move
    assert panel["rows"] == 0, "an FEA part has no opening rows"

    live.evaluate(f"document.querySelector('#gstage-{GROW_SESSION} .gc-proceed').click()")
    live.wait_for("window.__confirmed !== null", timeout=60, what="the answer to be confirmed")
    sent = live.evaluate("window.__confirmed")
    assert sent["size_mm"] == [2000.0, 900.0, 900.0], sent
    assert sent["part"] == "bracket" and sent["scale_to_m"] == 0.001, sent
    assert_clean(live, "an FEA panel grown on the routed payload")


#   -- the part-only panel is a panel, not a loose paragraph ------------------------------------

WAITING_SESSION = "waiting-panel-session"


def test_the_part_only_panel_carries_a_form_section_s_padding(live):
    """EVERY CUSTOMER WHO UPLOADS CAD SEES THIS PANEL, in the turn between the upload and their
    first answer. The waiting note is written into `.gc-form`, where the form's own sections will
    stand, and the box is marked `gc-waiting` - and theme.css had no rule for `gc-waiting` at all,
    so the note inherited the panel's own flow: no separation from the lead above it and no space
    under it. Measured in Chrome before: padding 0px on all four sides and no top rule."""
    _open_stage(live, WAITING_SESSION, _scouted_unrouted(), _skin())
    got = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{WAITING_SESSION}');
      const form = root.querySelector('.gc-form'), lead = root.querySelector('.gc-lead');
      const s = getComputedStyle(form);
      const num = (v) => Math.round(parseFloat(v) || 0);
      return {{waiting: root.classList.contains('gc-waiting'),
               note: !!form.querySelector('.gc-note'),
               top: num(s.paddingTop), bottom: num(s.paddingBottom),
               left: num(s.paddingLeft), right: num(s.paddingRight),
               rule: num(s.borderTopWidth),
               formLeft: Math.round(form.getBoundingClientRect().left),
               leadLeft: Math.round(lead.getBoundingClientRect().left),
               noteLeft: Math.round(form.querySelector('.gc-note').getBoundingClientRect().left)}};
    }})()""")
    assert got["waiting"] is True and got["note"] is True, got
    # a section of this panel, with the rhythm the form's own sections carry
    assert got["top"] > 0 and got["bottom"] > 0, got
    assert got["rule"] > 0, got
    # ...and on the lead's left edge, as every section of the stage's panel is (the CARD is what
    # pads sideways; the stage does not)
    assert got["left"] == 0 and got["right"] == 0, got
    assert got["noteLeft"] == got["leadLeft"] == got["formLeft"], got
    assert_clean(live, "the part-only panel")
