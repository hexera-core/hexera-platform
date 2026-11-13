# Responsibility: Verify the intake stays quiet and simple (Areen, 2026-11-13): the engine is chosen with no card
# and no lab numbers, the user's own words (in the chat or a note) still choose another, the chat is closed while
# the stage is up, a unit picked on the stage lets Proceed go on, and the confirmation reads as a short card.
# Boundaries: the page's own wiring (ui/js/main.js); only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui


def _json_fetch() -> str:
    return ("const json = (x, s) => Promise.resolve(new Response(JSON.stringify(x), "
            "{status: s || 200, headers: {'Content-Type': 'application/json'}}));")


def _declaration_external() -> str:
    from meshpipeline.api.v1.geometry import ConfirmIn
    from meshpipeline.application.geometry_confirmation import confirmation_message
    return confirmation_message(ConfirmIn(
        input_kind="solid-body", flow="external", unit="m", part="Camaro", flow_axis="+x",
        extents={"upstream": 5, "downstream": 10, "lateral": 3, "vertical": 4}, grounded=True,
        up_axis="+z", size_mm=[4836, 1920, 1350], reference_length_mm=4836))


def _declaration_internal() -> str:
    from meshpipeline.api.v1.geometry import ConfirmIn
    from meshpipeline.application.geometry_confirmation import confirmation_message
    return confirmation_message(ConfirmIn(
        input_kind="body-surface", flow="internal", unit="mm", part="duct", size_mm=[100, 50, 50],
        openings=[{"id": 1, "name": "inlet", "role": "inlet", "diameter_mm": 40},
                  {"id": 2, "name": "outlet", "role": "outlet", "diameter_mm": 40}]))


def _brief() -> dict:
    return {"purpose": "external_cfd", "purpose_label": "External CFD", "engine": "snappy",
            "engine_label": "snappyHexMesh", "input_kind": "solid-body", "input_kind_label": "Solid body",
            "label": "Camaro external aero", "submitted": False, "user_notes": [],
            "mesh_fidelity_label": "Standard (default)", "checks": []}


# ------------------------------------------------------------------------- the engine ----
def test_the_engine_is_chosen_with_no_card_and_the_plan_names_it_in_one_line(live):
    session = "clean-engine"
    live.evaluate("""(async () => {
      __JSON__
      window.__bodies = [];
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        if (String(u).includes('/api/v1/chat/message')) {
          window.__bodies.push(JSON.parse(o.body).content);
          // an older server still sends a choice payload: the console draws no card from it
          return json({session_id: '__S__', reply: 'What speed is the car going?', brief: __BRIEF__,
                       engine_choice: {proposed: 'snappy', engines: [{engine: 'snappy', label: 'snappyHexMesh',
                       fit: 'best fit', reason: 'passed 4 of 4 tries', reply: 'Use snappyHexMesh'}]}});
        }
        return real(u, o);
      };
      const st = await import('/static/js/core/state.js');
      st.set.sessionId('__S__');
      const c = await import('/static/js/shell/composer.js');
      await c.say('air over this car, external aero');
    })()""".replace("__JSON__", _json_fetch()).replace("__BRIEF__", json.dumps(_brief()))
       .replace("__S__", session), timeout=60)
    live.wait_for("document.querySelector('.req-card')", timeout=30, what="the plan card")
    seen = live.evaluate("""(() => ({
      card: document.querySelectorAll('.eng-choice, .ec-row, .ec-pick').length,
      engine: [...document.querySelectorAll('.req-card .rc-row')].filter(r => r.querySelector('.rc-k').textContent === 'Engine')
        .map(r => r.querySelector('.rc-v').textContent),
      text: document.getElementById('cc').innerText}))()""")
    assert seen["card"] == 0, seen
    assert seen["engine"] == ["snappyHexMesh"], seen
    for word in ("passed", "% ", "95% sure", "Heads-up", "Pick the mesh engine", "Why this engine"):
        assert word not in seen["text"], (word, seen["text"])
    assert_clean(live, "the plan with its engine line")


def test_a_note_that_says_use_cfmesh_is_said_in_the_chat_for_the_user(live):
    session = "clean-note"
    live.evaluate("""(async () => {
      __JSON__
      window.__bodies = [];
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/api/v1/chat/notes'))
          return json({session_id: '__S__', brief: __BRIEF__, say: 'Use cfMesh',
                       user_notes: [{id: 'n1', text: 'use cfMesh please', source: 'console'}]});
        if (s.includes('/api/v1/chat/message')) {
          window.__bodies.push(JSON.parse(o.body).content);
          return json({session_id: '__S__', reply: window.__bodies.length === 1 ? 'Here is the plan.' : 'Switched.',
                       brief: __BRIEF__});
        }
        if (s.includes('/check/parts')) return json({status: 'none'});
        return real(u, o);
      };
      const st = await import('/static/js/core/state.js');
      st.set.sessionId('__S__');
      const c = await import('/static/js/shell/composer.js');
      await c.say('external aero of this car');
    })()""".replace("__JSON__", _json_fetch()).replace("__BRIEF__", json.dumps(_brief()))
       .replace("__S__", session), timeout=60)
    live.wait_for("document.querySelector('.req-card .rc-notes-in')", timeout=30, what="the notes box")
    live.evaluate("""(() => {
      const inp = document.querySelector('.req-card .rc-notes-in');
      inp.value = 'use cfMesh please'; inp.dispatchEvent(new Event('input'));
      document.querySelector('.req-card .rc-notes-add').click();
    })()""")
    live.wait_for("window.__bodies.length === 2", timeout=30, what="the note's engine to be said in the chat")
    assert live.evaluate("window.__bodies") == ["external aero of this car", "Use cfMesh"]


# ---------------------------------------------------------------- the stage and the chat ----
def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (4.8, 1.9, 1.35))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _camaro_check() -> dict:
    # a triangle file with no unit, read as millimetres, and a size that makes the server doubt it
    return {"status": "ready", "skin": True, "pictures": [], "route": "cfd", "unit_needed": True,
            "proposal": {"part": "Camaro", "input_kind": "solid-body", "flow": "external",
                         "size_mm": [4836.0, 1920.0, 1350.0], "notes": [], "openings": [],
                         "flow_axis": "+x", "reference_length_mm": 4836.0, "grounded": True,
                         "extents": {"upstream": 5, "downstream": 10, "lateral": 3, "vertical": 4},
                         "scale_to_m": 0.001, "unit": "mm", "unit_basis": "assumed",
                         "unit_suggestion": {"unit": "in", "why": "a car this size is unusual",
                                             "sizes": {"mm": "4.84 m", "in": "123 m"}}}}


def _open_stage(live, session: str, chat_reply: str) -> None:
    live.evaluate("""(async () => {
      __JSON__
      const SKIN = __SKIN__, CHECK = __CHECK__;
      window.__bodies = []; window.__confirmed = null;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/geometry/__S__/check/skin')) return json(SKIN);
        if (s.includes('/geometry/__S__/check/confirm')) {
          window.__confirmed = JSON.parse(o.body);
          return json({ok: true, message: __DECL__, continued_with: 'I confirmed the geometry check. Go on.',
                       next: {session_id: '__S__', reply: 'What speed is the car going?', choices: []}});
        }
        if (s.endsWith('/geometry/__S__/check')) return json(window.__confirmed ? {...CHECK, confirmed: {}} : CHECK);
        if (s.includes('/api/v1/chat/message')) {
          window.__bodies.push(JSON.parse(o.body).content);
          return json({session_id: '__S__', reply: __REPLY__});
        }
        return real(u, o);
      };
      const st = await import('/static/js/core/state.js');
      st.set.sessionId('__S__');
      const c = await import('/static/js/shell/composer.js');
      c.enableInput();
      window.__watch = c.watchGeometryCheck('__S__');
    })()""".replace("__JSON__", _json_fetch()).replace("__SKIN__", json.dumps(_skin()))
       .replace("__CHECK__", json.dumps(_camaro_check())).replace("__DECL__", json.dumps(_declaration_external()))
       .replace("__REPLY__", json.dumps(chat_reply)).replace("__S__", session), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{session}']", timeout=90, what="the stage to open")


def test_the_chat_is_closed_while_the_stage_is_up_and_proceed_with_the_stage_unit_goes_on(live):
    session = "clean-stage"
    _open_stage(live, session, "I'll take inches - say ok, or name the unit.")
    box = live.evaluate("""(() => {const i = document.getElementById('chat-input');
      return {disabled: i.disabled, send: document.getElementById('send-btn').disabled, hint: i.placeholder};})()""")
    assert box == {"disabled": True, "send": True, "hint": "Finish on the panel →"}, box
    # a reply that lands while the stage is up does not open the box again
    live.evaluate("(async () => { const c = await import('/static/js/shell/composer.js'); await c.say('hello'); })()")
    live.wait_for("window.__bodies.length === 1", timeout=20, what="the message to be answered")
    assert live.evaluate("document.getElementById('chat-input').disabled") is True

    # THE UNIT IS PICKED ON THE STAGE. The box shows no unit while it is open, so picking millimetres
    # is a real pick (the box used to show millimetres already, and the pick changed nothing)
    unit = live.evaluate(f"""(() => {{
      const sel = document.querySelector('#gstage-{session} .gc-unit');
      return {{value: sel.value, first: sel.options[0].textContent}};
    }})()""")
    assert unit == {"value": "", "first": "pick the unit…"}, unit
    # Proceed before a pick says why and stays
    live.evaluate(f"document.querySelector('#gstage-{session} .gc-proceed').click()")
    assert live.evaluate("window.__confirmed") is None
    assert "Pick the file's unit first" in live.evaluate(f"document.querySelector('#gstage-{session} .gc-hint').textContent")
    live.evaluate(f"""(() => {{
      const sel = document.querySelector('#gstage-{session} .gc-unit');
      sel.value = 'mm'; sel.dispatchEvent(new Event('change'));
      document.querySelector('#gstage-{session} .gc-proceed').click();
    }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="Proceed to confirm with the stage's unit")
    assert live.evaluate("window.__confirmed.unit") == "mm"
    live.wait_for("!document.getElementById('chat-input').disabled", timeout=30, what="the chat to open again")

    # NO RAW DECLARATION: a short card, then the next question
    text = live.evaluate("document.getElementById('cc').innerText").replace("\xa0", " ")
    assert "✓ Confirmed: Camaro" in text and "4836 × 1920 × 1350 mm (file in metres)" in text, text
    assert "Flow around it, along +x" in text and "On the ground" in text, text
    for raw in ("input_kind", "solid-body", "reference lengths", "Far-field", "GEOMETRY CHECK",
                "wall patch named ground", "already converted", "Noted your changes"):
        assert raw not in text, (raw, text)
    assert "What speed is the car going?" in text
    assert_clean(live, "the stage, the closed chat and Proceed")


def test_a_declaration_reads_as_a_short_card_for_internal_flow_too(live):
    out = live.evaluate("""(async () => {
      const { displayText } = await import('/static/js/render/geometry_form.js');
      return displayText(__DECL__);
    })()""".replace("__DECL__", json.dumps(_declaration_internal())))
    assert out.splitlines() == ["**✓ Confirmed: duct**", "- 100 × 50 × 50 mm", "- Flow through it",
                                "- In: inlet · Out: outlet"], out
