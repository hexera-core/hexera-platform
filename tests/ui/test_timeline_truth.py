# Responsibility: Verify the run timeline says what happened - no tick beside a failure, a rebuild
# reads as sent back, only an attempt that ran a mesher is counted, and a reload keeps the clock.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Plays events through the page's own dispatch into the page's own stage, then reads the timeline
# back as a user sees it: each lane's name, its mark and its text, and the attempt headers.
PLAY = """
const { Stage } = await import('/static/js/render/stage.js');
const { dispatch, SENT_BACK, attemptsLabel } = await import('/static/js/core/events.js');
const play = (events) => { let c = null; for (const ev of events) c = dispatch(ev, Stage, c); return c; };
const MARKS = ['done', 'bad', 'back', 'warn', 'nomesh', 'ended', 'active'];
const read = () => ({
  lanes: [...document.querySelectorAll('#tl .tl-node')].map(n => ({
    name: n.querySelector('.nm').textContent,
    mark: MARKS.filter(m => n.querySelector('.tl-dot').classList.contains(m)).join(' '),
    title: n.querySelector('.tl-dot').title,
    dur: n.querySelector('.dur').textContent,
    text: n.querySelector('.tl-inner').textContent })),
  attempts: [...document.querySelectorAll('#tl .tl-att')].map(s => s.hidden ? '(hidden) ' + s.textContent : s.textContent),
  count: (document.querySelector('.tl-verd .mt') || {}).textContent || '',
  pill: (document.querySelector('.tl-verd .pill') || {}).textContent || '',
  head: document.querySelector('.proc') ? [...document.querySelector('.proc').classList] : [],
  clock: (document.getElementById('proc-clock') || {}).textContent || '',
});
Stage.mount();
"""


def run(page, body: str):
    return page.evaluate("(async () => {" + PLAY + body + "})()")


def attempt(n: int, *, built: bool, verdict: str | None = None) -> str:
    """One attempt's events, as the backend publishes them."""
    evs = [f"{{type:'attempt', stage:'builder', n:{n}, of:3}}",
           "{type:'note', stage:'builder', text:'Designing the mesh', tone:'info'}"]
    if built:
        evs += ["{type:'meshing', stage:'builder', engine:'snappy', budget_s:2400}",
                "{type:'meshed', stage:'builder', cells:3488033}"]
    # the backend's own closing line, said whether or not anything was built: the mark never reads it
    evs += ["{type:'note', stage:'builder', text:'Mesh built', tone:'info'}",
            "{type:'stage', stage:'executor'}",
            "{type:'note', stage:'executor', text:'Checking the mesh', tone:'info'}"]
    if verdict is None:
        evs.append("{type:'note', stage:'executor', text:'The mesh did not pass its checks', tone:'info'}")
    else:
        evs += ["{type:'check', stage:'executor', statement:'The mesh is structurally sound', ok:true}",
                "{type:'stage', stage:'reviewer'}",
                "{type:'rationale', stage:'reviewer', conclusion:'The reviewer asked for a rebuild.', because:'layers'}",
                f"{{type:'verdict', stage:'reviewer', verdict:'{verdict}'}}"]
    return ",".join(evs)


START = ("{type:'stage', stage:'engine_select'},"
         "{type:'note', stage:'engine_select', text:'Mesh engine: snappy', tone:'info'},")


def marks(out) -> list[tuple[str, str]]:
    return [(lane["name"], lane["mark"]) for lane in out["lanes"]]


def test_a_review_that_sends_the_mesh_back_reads_sent_back_never_a_tick(live):
    # The Supra run of 2026-10-03: two reviews failed the mesh and asked for a rebuild, the third
    # left points open and the mesh was delivered with concerns. Each FAIL used to close with a
    # green tick beside it.
    out = run(live, f"""
      play([{START}{attempt(1, built=True, verdict='FAIL')},{attempt(2, built=True, verdict='FAIL')},
            {{type:'stage', stage:'outcome'}},
            {{type:'note', stage:'outcome', text:'Packaged your mesh', tone:'info'}}]);
      Stage.final({{pass:true, reviewOutcome:'delivered_with_concerns', attempts:2, text:''}});
      return {{...read(), SENT_BACK}};""")
    assert marks(out) == [
        ("Engine selection", "done"),
        ("Mesh creation", "done"), ("Mesh validation", "done"), ("Review", "back"),
        ("Mesh creation", "done"), ("Mesh validation", "done"), ("Review", "warn"),
        ("Outcome", "done"), ("Result", "warn")], marks(out)
    sent_back = out["lanes"][3]
    assert out["SENT_BACK"] in sent_back["text"] and sent_back["title"] == "sent back for a rebuild"
    assert "left points open" not in sent_back["text"], "a rebuild request still read as the run's last word"
    assert out["attempts"] == ["Attempt 1", "Attempt 2"]
    assert out["pill"] == "DELIVERED WITH CONCERNS" and out["count"] == "2 attempts"
    assert "r-concerns" in out["head"], "the header's mark did not take the verdict's colour"


def test_a_failed_run_never_wears_a_tick_on_its_review_its_result_or_its_header(live):
    out = run(live, f"""
      play([{START}{attempt(1, built=True, verdict='FAIL')}]);
      Stage.final({{pass:false, attempts:1, text:''}});
      return read();""")
    assert marks(out) == [("Engine selection", "done"), ("Mesh creation", "done"),
                          ("Mesh validation", "done"), ("Review", "bad"), ("Result", "bad")], marks(out)
    assert out["pill"] == "FAIL" and "r-fail" in out["head"]


def test_attempts_that_never_ran_a_mesher_are_not_counted_as_attempts(live):
    # The aorta STL runs of 2026-10-03: both attempts were refused before meshing, and the result
    # still said "2 attempts" under ticked "Mesh creation" lanes.
    out = run(live, f"""
      play([{START}{attempt(1, built=False)},{attempt(2, built=False)}]);
      Stage.final({{pass:false, attempts:2, text:''}});
      return read();""")
    assert out["count"] == "no mesh was built", out["count"]
    assert out["attempts"] == ["Attempt 1 · no mesh built", "Attempt 2 · no mesh built"]
    assert marks(out) == [("Engine selection", "done"),
                          ("Mesh creation", "nomesh"), ("Mesh validation", "back"),
                          ("Mesh creation", "nomesh"), ("Mesh validation", "bad"),
                          ("Result", "bad")], marks(out)


def test_only_the_attempts_that_ran_a_mesher_are_counted(live):
    out = run(live, f"""
      play([{START}{attempt(1, built=True)},
            {{type:'note', stage:'builder', text:'The mesh came back with defects - reworking it', tone:'warn'}},
            {attempt(2, built=False)}]);
      Stage.final({{pass:false, attempts:2, text:''}});
      return read();""")
    assert out["count"] == "1 attempt", out["count"]
    assert out["attempts"] == ["Attempt 1", "Attempt 2 · no mesh built"]


def test_the_attempt_count_says_what_was_built(live):
    out = live.evaluate("""(async () => {
      const { attemptsLabel } = await import('/static/js/core/events.js');
      return [attemptsLabel(true, 0, 2), attemptsLabel(true, 1, 2), attemptsLabel(true, 3, 3),
              attemptsLabel(false, 0, 2), attemptsLabel(false, 0, 0)];
    })()""")
    assert out == ["no mesh was built", "1 attempt", "3 attempts", "2 attempts", ""], \
        "a run the timeline never saw keeps the server's count; one it saw counts what it built"


def test_warnings_errors_and_failed_checks_mark_their_lane(live):
    out = run(live, """
      play([{type:'stage', stage:'geometry_admission'},
            {type:'note', stage:'geometry_admission', text:'Input rejected', tone:'error'},
            {type:'stage', stage:'executor'},
            {type:'check', stage:'executor', statement:'Your geometry can be meshed by this engine', ok:false}]);
      Stage.final({pass:false, attempts:0, text:''});
      return read();""")
    assert marks(out) == [("Input validation", "bad"), ("Mesh validation", "bad"),
                          ("Result", "bad")], marks(out)
    assert out["count"] == "", "a run with no attempt events invented a count"


def test_a_cancelled_run_stops_its_open_lane_and_its_mesh_bar(live):
    out = run(live, f"""
      play([{START}{{type:'attempt', stage:'builder', n:1, of:3}},
            {{type:'meshing', stage:'builder', engine:'snappy', budget_s:600}}]);
      Stage.final({{pass:false, cancelled:true, attempts:1, text:''}});
      const bar = document.querySelector('.meshbar');
      return {{...read(), barOpen: Stage.meshBar !== null,
               barGreen: bar.classList.contains('done'), barSub: bar.querySelector('.mb-sub').textContent}};""")
    assert ("Mesh creation", "ended") in marks(out) and ("Result", "ended") in marks(out), marks(out)
    assert out["pill"] == "CANCELLED" and "r-cancelled" in out["head"]
    assert not out["barOpen"] and not out["barGreen"], "a mesh bar kept counting after the run ended"
    assert out["barSub"] == "stopped when the run ended"


def test_a_run_ends_once_however_many_polls_see_it_end(live):
    out = run(live, """
      play([{type:'stage', stage:'builder'}]);
      Stage.final({pass:false, attempts:1, text:'it failed'});
      Stage.final({pass:false, attempts:1, text:'it failed'});
      return {rows: document.querySelectorAll('.tl-verd').length,
              said: [...document.querySelectorAll('#cc .im.assistant .txt')].filter(t => t.textContent === 'it failed').length};""")
    assert out == {"rows": 1, "said": 1}, out


def test_a_new_run_on_the_same_page_starts_clean(live):
    # The last run ended mid-mesh on its first attempt. The next run's first attempt header went
    # missing (the stage still thought it was on attempt 1), and its mesh bar never appeared.
    out = run(live, """
      play([{type:'attempt', stage:'builder', n:1, of:3},
            {type:'meshing', stage:'builder', engine:'snappy', budget_s:600}]);
      Stage.mount();
      play([{type:'attempt', stage:'builder', n:1, of:3},
            {type:'meshing', stage:'builder', engine:'snappy', budget_s:600},
            {type:'meshed', stage:'builder', cells:10},
            {type:'attempt', stage:'builder', n:2, of:3}]);
      return {...read(), bars: document.querySelectorAll('.meshbar').length};""")
    assert out["attempts"] == ["Attempt 1", "Attempt 2"], out["attempts"]
    assert out["bars"] == 1, "the new run's mesh bar did not appear"


def test_a_re_review_says_it_started(live):
    # The re-review's message was drawn and then wiped by the stage the new run mounted.
    out = live.evaluate("""(async () => {
      const real = window.fetch;
      window.fetch = async (url) => String(url).includes('/dispute')
        ? new Response(JSON.stringify({job_id:'job-rerun', dispute_of:'job-a', flags:0}), {status:200})
        : new Response(JSON.stringify({detail:'not here'}), {status:404});
      try {
        const D = await import('/static/js/viewer/dispute.js');
        D.acceptMesh('job-a', ['layers']);
        const ta = document.getElementById('v-acc-note');
        ta.value = '17% coverage is fine for a pressure study';
        ta.dispatchEvent(new Event('input'));
        document.getElementById('v-acc-go').click();
        await new Promise(r => setTimeout(r, 300));
        return [...document.querySelectorAll('#cc .im.assistant .txt')].map(t => t.textContent);
      } finally { window.fetch = real; }
    })()""")
    assert any("the same mesh" in t for t in out), out
