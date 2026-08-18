# Responsibility: Verify the browser renders everything the backend publishes, live and on replay, identically.
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

# A renderer that records calls instead of drawing. Every method the dispatch may reach.
FAKE = """
const calls = [];
const R = new Proxy({}, { get: (_t, prop) => (...a) => calls.push([String(prop), ...a]) });
"""


def test_a_run_is_interpreted_into_the_cards_and_lanes_a_user_sees(live):
    out = live.evaluate(FAKE + """(async () => {
      const { dispatch } = await import('/static/js/core/events.js');
      let c = null;
      for (const ev of [
        {type:'attempt', n:1},
        {type:'stage', stage:'builder'},
        {type:'tool_call', stage:'builder', id:'c1', tool_name:'write_file'},
        {type:'tool_result', stage:'builder', tool_call_id:'c1', status:'success'},
        {type:'file', stage:'builder', display_path:'system/meshDict',
         byte_count:2418, operation:'created'},
        {type:'check', statement:'no stage of its own - it belongs to the open lane', ok:true},
        {type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600},
        {type:'meshed', stage:'builder', cells:3907590},
        {type:'stage', stage:'executor'},
        {type:'check', stage:'executor', statement:'mesh is solvable', ok:true},
        {type:'verdict', stage:'reviewer', verdict:'PASS'},
        {type:'closing', text:'your mesh is ready'},
      ]) c = dispatch(ev, R, c);
      return {calls, cursor: c};
    })()""")
    calls = out["calls"]
    named = {c[0] for c in calls}
    assert {"attempt", "toolCall", "toolResult", "file", "startMesh", "endMesh", "check"} <= named

    assert next(c for c in calls if c[0] == "file")[3] == "2.4 KB", "a raw byte count reached a card"
    assert next(c for c in calls if c[0] == "endMesh")[1] == "3,907,590 cells"

    # A card with no stage of its own belongs to whichever lane is open.
    stray = next(c for c in calls if c[0] == "check" and "no stage of its own" in str(c[2]))
    assert stray[1] == "builder", "a card was filed under no lane"

    # A stage becoming active retires the one before it, so no lane is left open behind.
    assert [c[1] for c in calls if c[0] == "done"] == ["builder", "executor"]

    verdict = next(c for c in calls if c[0] == "info" and "Verdict" in str(c[2]))
    assert "meets your brief" in verdict[2] and verdict[3] is False, \
        "a PASS was worded or toned as a failure"
    assert not any("closing" in str(c) for c in calls), \
        "`closing` drew a card - the result card owns that text"
    assert out["cursor"] == "reviewer"


def test_no_frame_the_backend_can_send_stops_the_timeline(live):
    out = live.evaluate(FAKE + """(async () => {
      const { dispatch } = await import('/static/js/core/events.js');
      const junk = [null, undefined, "a string", 42, {}, {type:7}, {type:'invented_next_year'},
                    {stage:'builder'}, {type:'check'}];
      let c = dispatch({type:'stage', stage:'builder'}, R, null);
      for (const bad of junk) c = dispatch(bad, R, c);
      c = dispatch({type:'check', stage:'builder', statement:'still rendering', ok:true}, R, c);
      return {names: calls.map(x => x[0]), cursor: c};
    })()""")
    assert "check" in out["names"], "a frame the UI did not understand stopped the stream"
    assert out["cursor"] == "builder", "an unusable frame moved the lane cursor"


def test_a_replayed_run_renders_exactly_as_the_live_one_did(live):
    out = live.evaluate(FAKE + """(async () => {
      const { dispatch } = await import('/static/js/core/events.js');
      const events = [
        {type:'stage', stage:'builder', seq:1},
        {type:'reasoning', stage:'builder', id:'r1', phase:'started', seq:2},
        {type:'reasoning', stage:'builder', id:'r1', phase:'completed', duration_ms:8400, seq:3},
        {type:'tool_call', stage:'builder', id:'c1', tool_name:'run_mesh', seq:4},
        {type:'tool_result', stage:'builder', tool_call_id:'c1', status:'success', seq:5},
        {type:'file', stage:'builder', display_path:'a', byte_count:9, operation:'created', seq:6},
        {type:'rationale', stage:'builder', conclusion:'ready', because:'gates passed', seq:7},
      ];
      const play = () => { calls.length = 0; let c = null;
        for (const ev of events) c = dispatch(ev, R, c);
        return JSON.parse(JSON.stringify(calls)); };
      const live = play(), replayed = play();
      return {same: JSON.stringify(live) === JSON.stringify(replayed), calls: live.length};
    })()""")
    assert out["same"], "a replayed run produced different renderer calls from the live one"
    assert out["calls"] >= 6


def test_the_browser_can_render_everything_the_backend_publishes(live, event_vocabulary):
    types, stages = event_vocabulary
    # The stage list plus one the browser has never been told about. EVERY stage the backend
    # ships today has a label, so without this the degradation path - the one that decides
    # whether adding a stage requires a UI change - is never exercised.
    stages = (*stages, "adaptive_refinement_sweep")
    out = live.evaluate(FAKE + """(async () => {
      const { dispatch, laneLabel } = await import('/static/js/core/events.js');
      const [types, stages] = __VOCABULARY__;
      const rendered = {};
      for (const t of types) {
        calls.length = 0;
        dispatch({type:t, stage:'builder', n:1, statement:'s', text:'t', ok:true,
                  actions:['did a thing'], query:'q', image:'AAAA', display_path:'p',
                  byte_count:1, operation:'created', id:'i', tool_call_id:'i',
                  tool_name:'tool', phase:'completed', conclusion:'c', because:'b',
                  engine:'e', budget_s:1, cells:2, verdict:'PASS'}, R, 'builder');
        rendered[t] = calls.length;
      }
      return {rendered, labels: Object.fromEntries(stages.map(s => [s, laneLabel(s)]))};
    })()""".replace("__VOCABULARY__", json.dumps([list(types), list(stages)])))

    # `closing` is held by the stream for the result card rather than drawn; `stage` opens a
    # lane. Everything else must put something on screen.
    silent = [t for t, n in out["rendered"].items() if n == 0 and t not in ("closing", "stage")]
    assert not silent, (
        f"the backend publishes {sorted(silent)} and the browser draws nothing for them - "
        f"a user would see their run stall")

    for stage, label in out["labels"].items():
        assert "_" not in label and label[:1].isupper(), \
            f"stage {stage!r} reaches the user as a raw key: {label!r}"
    assert out["labels"]["adaptive_refinement_sweep"] == "Adaptive refinement sweep", \
        "a stage the UI has no word for must degrade to English, not require a UI release"


def test_a_tool_call_is_labelled_TOOL_CALL_and_reasoning_fills_one_card(live):
    # What the label SAYS, read off the rendered page rather than the source. The tag is uppercased
    # by the stylesheet, so the text a user sees is not the text the template holds.
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();                       // a clean stage, in the page's own #stage element
      Stage.toolCall('builder', {id:'c1', tool_name:'write_case_file', status:'started'});
      const tag = document.querySelector('.e-tool .tag');
      const shown = getComputedStyle(tag).textTransform === 'uppercase'
                    ? tag.textContent.toUpperCase() : tag.textContent;

      // reasoning: three deltas against ONE id must leave ONE card holding the latest text
      Stage.reasoning('builder', {id:'r1', phase:'started', content:'Checking '});
      Stage.reasoning('builder', {id:'r1', phase:'started', content:'Checking the budget'});
      Stage.reasoning('builder', {id:'r1', phase:'completed', content:'Checking the budget.',
                                  duration_ms:8400});
      const cards = document.querySelectorAll('.e-think');
      return {shown, cards: cards.length,
              body: cards.length ? cards[cards.length-1].querySelector('.body').textContent : ''};
    })()""")
    assert out["shown"] == "TOOL CALL", f"a tool call is labelled {out['shown']!r}"
    assert out["cards"] == 1, f"streamed reasoning drew {out['cards']} cards instead of filling one"
    assert out["body"] == "Checking the budget."
