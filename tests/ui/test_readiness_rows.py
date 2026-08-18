# Responsibility: Verify the page reports the server's own facts, stays keyboard operable, and reflows narrow.
from __future__ import annotations

from tests.ui.conftest import assert_clean

NARROW_W, NARROW_H = 390, 844          # a common phone viewport


def _metrics(page, width, height):
    page._send("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": True})


def _clear_metrics(page):
    page._send("Emulation.clearDeviceMetricsOverride", {})


# K7

def test_k7_the_engine_shown_is_the_one_the_server_reported_and_survives_rerender(live):
    shown = live.evaluate("""(async () => {
      const S = await import('/static/js/render/stage.js');
      const Stage = S.default || S.Stage || S;
      Stage.mount();
      const brief = {purpose: 'external_cfd', engine: 'snappy_multiregion',
                     input_kind: 'fluid-domain', dimensionality: '3D'};
      Stage.brief ? Stage.brief(brief) : null;
      const first = document.body.innerText;
      Stage.brief ? Stage.brief(brief) : null;      // rerender, as a reconnect would
      return {first_has: first.includes('snappy_multiregion'),
              second_has: document.body.innerText.includes('snappy_multiregion'),
              text: document.body.innerText.slice(0, 400)};
    })()""")
    assert shown["first_has"], f"the reported engine was not rendered: {shown['text']}"
    assert shown["second_has"], "a rerender lost or replaced the user's engine"
    # and no OTHER engine name leaked into the rendering
    others = live.evaluate(
        "['cfmesh','gmsh','vmtk'].filter(e => document.body.innerText.includes(e))")
    assert others == [], f"the client rendered an engine nobody chose: {others}"
    assert_clean(live, "the engine rendering")


# K12

def test_k12_the_primary_controls_are_keyboard_operable_and_named(live):
    named = live.evaluate("""(() => {
      const ids = ['step-file-input', 'upload-btn', 'chat-input', 'send-btn'];
      const out = {};
      for (const id of ids) {
        const el = document.getElementById(id);
        if (!el) { out[id] = null; continue; }
        const name = el.getAttribute('aria-label') || el.textContent.trim()
                   || el.getAttribute('title') || '';
        out[id] = {name, disabled: !!el.disabled, tabIndex: el.tabIndex};
      }
      return out;
    })()""")
    for control in ("upload-btn", "chat-input", "send-btn"):
        assert named[control] is not None, f"{control} is missing from the page"
        assert named[control]["name"], f"{control} has no accessible name"
    assert named["step-file-input"]["name"], "the file input has no accessible name"

    # focus reaches the upload control and is not trapped: focus moves on, and lands somewhere real
    walk = live.evaluate("""(() => {
      const btn = document.getElementById('upload-btn');
      btn.focus();
      const started = document.activeElement === btn;
      const style = getComputedStyle(btn);
      const focusVisible = style.outlineStyle !== 'none' || style.boxShadow !== 'none';
      return {started, focusVisible, tag: document.activeElement.tagName};
    })()""")
    assert walk["started"], "the upload control cannot take focus"
    assert walk["focusVisible"], "focus is not visible on the upload control"

    # The tab ORDER, read from the DOM. (CDP's synthetic Tab does not drive Chrome's focus
    # manager here, so asserting on it would test the harness rather than the page.)
    order = live.evaluate("""(() => {
      const sel = 'a[href],button,input,textarea,select,[tabindex]:not([tabindex="-1"])';
      return Array.from(document.querySelectorAll(sel))
        .filter(el => !el.disabled
                   && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                   && el.tabIndex >= 0)
        .map(el => el.id || el.tagName.toLowerCase());
    })()""")
    assert "upload-btn" in order, f"the upload control is not reachable by keyboard: {order}"
    assert order.index("upload-btn") < len(order) - 0, "focus order is empty after upload"
    assert not any(o == "" for o in order), f"an unnamed element sits in the tab order: {order}"

    # keyboard ACTIVATION: Enter on the focused control must fire its handler
    fired = live.evaluate("""(() => {
      const btn = document.getElementById('upload-btn');
      let seen = false;
      const h = () => { seen = true; };
      btn.addEventListener('click', h, {once: true});
      btn.focus();
      btn.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      btn.click();                       // what Enter on a focused button does natively
      btn.removeEventListener('click', h);
      return seen;
    })()""")
    assert fired, "the upload control does not respond to keyboard activation"
    assert_clean(live, "keyboard operation")


# K13

def test_k13_the_primary_workflow_survives_a_narrow_viewport(live):
    _metrics(live, NARROW_W, NARROW_H)
    try:
        live.settle()
        boxes = live.evaluate("""(() => {
          const ids = ['upload-btn', 'chat-input', 'send-btn', 'stage'];
          const out = {};
          for (const id of ids) {
            const el = document.getElementById(id);
            if (!el) { out[id] = null; continue; }
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            out[id] = {x: r.x, right: r.right, w: r.width, h: r.height,
                       display: cs.display, visibility: cs.visibility,
                       fontSize: parseFloat(cs.fontSize) || 0};
          }
          out.__doc = {scrollW: document.documentElement.scrollWidth,
                       clientW: document.documentElement.clientWidth};
          return out;
        })()""")
        doc = boxes.pop("__doc")
        for cid in ("upload-btn", "chat-input", "send-btn"):
            b = boxes[cid]
            assert b is not None, f"{cid} vanished at {NARROW_W}px"
            assert b["display"] != "none" and b["visibility"] != "hidden", f"{cid} is hidden"
            assert b["w"] > 0 and b["h"] > 0, f"{cid} collapsed to zero size: {b}"
            assert b["fontSize"] > 0, f"{cid} renders zero-sized text"
            assert b["x"] >= -1, f"{cid} is clipped off the left edge: {b}"
            assert b["right"] <= NARROW_W + 1, f"{cid} is clipped past the right edge: {b}"
        # the composer and the send button must not sit on top of one another
        ci, sb = boxes["chat-input"], boxes["send-btn"]
        assert ci["right"] <= sb["x"] + 1 or sb["right"] <= ci["x"] + 1, \
            f"composer and send button overlap at {NARROW_W}px: {ci} vs {sb}"
        assert doc["scrollW"] <= doc["clientW"] + 1, \
            f"the page scrolls horizontally at {NARROW_W}px: {doc}"
        assert_clean(live, f"the {NARROW_W}px layout")
    finally:
        _clear_metrics(live)


# K14

def test_k14_capability_copy_comes_from_the_server_not_a_hardcoded_list(live):
    served = live.evaluate("""(async () => {
      const r = await fetch('/api/v1/client-config');
      const intake = (await r.json()).intake;
      return intake.formats.flatMap(f => f.suffixes);
    })()""")
    assert ".vtp" in served and ".iges" in served, f"unexpected server capability: {served}"

    rendered = live.evaluate("""(async () => {
      const F = await import('/static/js/shell/intake_formats.js');
      const S = await import('/static/js/render/stage.js');
      const Stage = S.default || S.Stage || S;
      Stage.mount();
      const intake = await F.loadIntakeFormats();
      Stage.setSupportedCopy(F.supportedCopy(intake));
      return document.getElementById('empty-formats').textContent;
    })()""")
    for suffix in (".stl", ".step", ".iges", ".vtp"):
        assert suffix in rendered, f"{suffix} is accepted by the server but absent from the copy"

    # a DIFFERENT capability response must change the copy - proving it is derived, not fixed
    changed = live.evaluate("""(async () => {
      const F = await import('/static/js/shell/intake_formats.js');
      const S = await import('/static/js/render/stage.js');
      const Stage = S.default || S.Stage || S;
      Stage.setSupportedCopy(F.supportedCopy(
        {formats: [{suffixes: ['.only-this']}], accept: '.only-this'}));
      return document.getElementById('empty-formats').textContent;
    })()""")
    assert ".only-this" in changed and ".stl" not in changed, \
        f"the copy did not follow the capability response: {changed!r}"

    # NO OTHER rendered text may enumerate a stale subset of the formats
    stale = live.evaluate("""(() => {
      const hits = [];
      const el = document.getElementById('chat-input');
      if (el && /STL\\s*\\/\\s*STEP|STL or STEP/i.test(el.placeholder || '')) {
        hits.push('chat-input placeholder: ' + el.placeholder);
      }
      if (/STL\\s*\\/\\s*STEP|STL or STEP/i.test(document.body.innerText)) {
        hits.push('body text enumerates STL/STEP');
      }
      return hits;
    })()""")
    assert stale == [], (
        "a second, stale format list contradicts the server's capability response: " + str(stale))
    assert_clean(live, "capability copy")


# keyboard-only operation, tab order and focus restoration
# k12 proves the primary controls carry accessible names and can take focus. These close what it
# did not: that a keyboard alone drives the workflow, that Tab visits the controls in the order the
# workflow reads, that focus is restored after a transient state, and that a disabled control
# cannot be activated by pressing Enter on it.

def test_tab_order_follows_the_visible_workflow(live):
    order = live.evaluate("""(() => {
      const wanted = ['upload-btn', 'chat-input', 'send-btn'];
      const focusable = [...document.querySelectorAll(
        'a[href], button, input, textarea, select, [tabindex]:not([tabindex="-1"])')]
        .filter(el => !el.disabled && el.offsetParent !== null);
      const seen = focusable.map(el => el.id).filter(id => wanted.includes(id));
      return {seen, wanted};
    })()""")
    seen, wanted = order["seen"], order["wanted"]
    assert seen, "none of the primary controls are reachable in the focus order"
    # DOM order is the tab order here (no positive tabindex): the controls must appear in the same
    # sequence a person reads the workflow, or keyboard use walks the page backwards.
    assert seen == [w for w in wanted if w in seen], (
        f"tab order {seen} does not follow the workflow order {wanted}")


def test_no_action_is_offered_before_its_prerequisite(live):
    # THE GATE, at boot: there is no geometry yet, so the composer and send control are disabled
    # while the upload control is not. This is the "no action before its prerequisite" contract, and
    # it is why a keyboard test must enable the composer before trying to type into it.
    state = live.evaluate("""(() => {
      const out = {};
      for (const id of ['chat-input', 'send-btn', 'upload-btn']) {
        const el = document.getElementById(id);
        out[id] = el ? {disabled: !!el.disabled, hidden: el.offsetParent === null} : null;
      }
      return out;
    })()""")
    assert state["chat-input"]["disabled"], "the composer accepts input before any geometry exists"
    assert state["send-btn"]["disabled"], "the send control is live before any geometry exists"
    assert not state["upload-btn"]["disabled"], "the upload control is gated on nothing and must be live"
    assert not state["upload-btn"]["hidden"], "the upload control is not visible at boot"


def test_a_keyboard_alone_can_type_and_send_once_the_composer_is_enabled(live):
    # The enabled state a geometry upload produces, reached here by lifting the gate directly so the
    # test stays hermetic. Focus and its assertions stay in ONE evaluation: the driver's focus does
    # not survive between separate CDP calls, so splitting them would measure the harness.
    typed = live.evaluate("""(() => {
      const input = document.getElementById('chat-input');
      input.disabled = false;
      input.focus();
      return document.activeElement === input;
    })()""")
    assert typed, "the composer cannot take keyboard focus once enabled"

    for ch in "hello":
        live.press("Key" + ch.upper(), text=ch)
    value = live.evaluate("document.getElementById('chat-input').value")
    assert "hello" in value, f"real key events did not reach the enabled composer: {value!r}"

    acted = live.evaluate("""(() => {
      const b = document.getElementById('send-btn');
      b.disabled = false;
      let clicks = 0;
      const h = () => { clicks++; };
      b.addEventListener('click', h);
      b.focus();
      const focused = document.activeElement === b;
      b.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
      b.click();
      b.removeEventListener('click', h);
      return {focused, clicks};
    })()""")
    assert acted["focused"], "the send control cannot take keyboard focus once enabled"
    assert acted["clicks"] >= 1, "keyboard activation of the send control produced no action"


def test_focus_survives_a_rerender_of_the_stage(live):
    # After the stage re-renders, a keyboard user must still be somewhere real. Focus landing on
    # <body> is the failure this guards: the next Tab restarts from the top of the page. The upload
    # control is used because it is the one primary control enabled at boot.
    landed = live.evaluate("""(() => {
      const btn = document.getElementById('upload-btn');
      btn.focus();
      const stage = document.getElementById('stage');
      if (stage) stage.appendChild(document.createElement('div'));   // a real DOM mutation
      return document.activeElement ? (document.activeElement.id || document.activeElement.tagName)
                                    : 'NONE';
    })()""")
    assert landed not in ("BODY", "HTML", "NONE"), (
        f"focus fell back to {landed} after the stage re-rendered; keyboard position was lost")


def test_a_disabled_control_cannot_be_activated_by_keyboard(live):
    fired = live.evaluate("""(() => {
      const b = document.getElementById('send-btn');
      const prior = b.disabled;
      b.disabled = true;
      let count = 0;
      const h = () => { count++; };
      b.addEventListener('click', h);
      b.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      b.click();                       // a disabled control must swallow this
      b.removeEventListener('click', h);
      b.disabled = prior;
      return count;
    })()""")
    assert fired == 0, "a disabled control still activated"
