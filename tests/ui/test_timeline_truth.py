# Responsibility: Verify the run box says what happened - four rows for every run, no tick beside a
# failure, a rebuild reads as sent back, only an attempt that ran a mesher is counted, and one ending.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Plays events through the page's own dispatch into the page's own stage, then reads the box back as
# a user sees it: each row's name, its mark, its time, the lines in view and its folded steps.
PLAY = """
const { Stage } = await import('/static/js/render/stage.js');
const { dispatch, attemptsLabel } = await import('/static/js/core/events.js');
const play = (events) => { let c = null; for (const ev of events) c = dispatch(ev, Stage, c); return c; };
const MARKS = ['done', 'bad', 'back', 'warn', 'nomesh', 'skip', 'ended', 'active'];
const read = () => ({
  lanes: [...document.querySelectorAll('#tl .tl-node')].map(n => ({
    name: n.querySelector('.nm').textContent,
    mark: MARKS.filter(m => n.querySelector('.tl-dot').classList.contains(m)).join(' '),
    title: n.querySelector('.tl-dot').title,
    dur: n.querySelector('.dur').textContent,
    said: n.querySelector('.rr-said').textContent,
    text: n.querySelector('.tl-inner').textContent })),
  attempts: [...document.querySelectorAll('#tl .tl-att')].map(s => s.textContent),
  head: document.querySelector('.proc') ? [...document.querySelector('.proc').classList] : [],
  title: (document.querySelector('.proc-title') || {}).textContent || '',
  count: (document.querySelector('.proc') || {dataset: {}}).dataset.attempts || '',
  clock: (document.getElementById('proc-clock') || {}).textContent || '',
});
Stage.mount();
"""

ROWS = ["Choosing the engine", "Designing the mesh", "Creating the mesh", "Reviewing the mesh"]


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


def test_every_run_shows_the_same_four_rows_and_no_result_row(live):
    out = run(live, f"""
      play([{START}{attempt(1, built=True, verdict='PASS')}]);
      Stage.final({{pass:true, attempts:1, text:''}});
      return read();""")
    assert [lane["name"] for lane in out["lanes"]] == ROWS
    # one time per row, and nothing else counts: no "N steps" beside a name
    assert all(lane["dur"] for lane in out["lanes"] if lane["mark"] != "skip"), out["lanes"]
    assert out["lanes"][0]["said"] == "snappy"
    # a run that said nothing about its design still shows the row, saying so
    assert out["lanes"][1]["mark"] == "skip" and "Nothing to design" in out["lanes"][1]["said"]
    assert out["lanes"][2]["said"] == "Mesh built: 3.5M cells."
    assert "The check passed." in out["lanes"][3]["said"]


def test_a_review_that_sends_the_mesh_back_reads_sent_back_never_a_tick(live):
    # The Supra run of 2026-10-03: two reviews failed the mesh and asked for a rebuild, the third
    # left points open and the mesh was delivered with concerns. Each FAIL used to close with a
    # green tick beside it.
    out = run(live, f"""
      play([{START}{attempt(1, built=True, verdict='FAIL')},{attempt(2, built=True, verdict='FAIL')}]);
      const between = read();
      play([{{type:'attempt', stage:'builder', n:3, of:3}}]);
      const sent = read();
      Stage.mount();
      play([{START}{attempt(1, built=True, verdict='FAIL')},{attempt(2, built=True, verdict='FAIL')},
            {{type:'stage', stage:'outcome'}},
            {{type:'note', stage:'outcome', text:'Packaged your mesh', tone:'info'}}]);
      Stage.final({{pass:true, reviewOutcome:'delivered_with_concerns', attempts:2, text:''}});
      return {{between, sent, end: read()}};""")
    # while the rebuild runs, the review that sent it back says so - never a tick
    review = out["sent"]["lanes"][3]
    assert review["mark"] == "back" and review["title"] == "sent back for a rebuild", review
    assert "Attempt 3: the review asked for changes in attempt 2" in out["sent"]["lanes"][2]["said"]
    end = out["end"]
    assert marks(end) == [(ROWS[0], "done"), (ROWS[1], "skip"), (ROWS[2], "done"), (ROWS[3], "warn")], marks(end)
    assert end["attempts"].count("Attempt 2") == 3, "each row's steps set the second attempt apart"
    assert "Took 2 attempts." in end["lanes"][2]["said"]
    assert end["title"] == "Mesh ready, with concerns" and end["count"] == "2 attempts"
    assert "r-concerns" in end["head"], "the header's mark did not take the verdict's colour"


def test_a_failed_run_never_wears_a_tick_on_its_review_or_its_header(live):
    out = run(live, f"""
      play([{START}{attempt(1, built=True, verdict='FAIL')}]);
      Stage.final({{pass:false, attempts:1, text:''}});
      return read();""")
    assert marks(out) == [(ROWS[0], "done"), (ROWS[1], "skip"), (ROWS[2], "done"), (ROWS[3], "bad")], marks(out)
    assert out["title"] == "Run failed" and "r-fail" in out["head"]


def test_attempts_that_never_ran_a_mesher_are_not_counted_as_attempts(live):
    # The aorta STL runs of 2026-10-03: both attempts were refused before meshing, and the result
    # still said "2 attempts" under ticked "Mesh creation" lanes.
    out = run(live, f"""
      play([{START}{attempt(1, built=False)},{attempt(2, built=False)}]);
      Stage.final({{pass:false, attempts:2, text:''}});
      return read();""")
    assert out["count"] == "no mesh was built", out["count"]
    build = out["lanes"][2]
    assert build["mark"] == "nomesh" and "nothing was built in attempt 1" in build["said"], build


def test_only_the_attempts_that_ran_a_mesher_are_counted(live):
    out = run(live, f"""
      play([{START}{attempt(1, built=True)},
            {{type:'note', stage:'builder', text:'The mesh came back with defects - reworking it', tone:'warn'}},
            {attempt(2, built=False)}]);
      Stage.final({{pass:false, attempts:2, text:''}});
      return read();""")
    assert out["count"] == "1 attempt", out["count"]


def test_the_attempt_count_says_what_was_built(live):
    out = live.evaluate("""(async () => {
      const { attemptsLabel } = await import('/static/js/core/events.js');
      return [attemptsLabel(true, 0, 2), attemptsLabel(true, 1, 2), attemptsLabel(true, 3, 3),
              attemptsLabel(false, 0, 2), attemptsLabel(false, 0, 0)];
    })()""")
    assert out == ["no mesh was built", "1 attempt", "3 attempts", "2 attempts", ""], \
        "a run the timeline never saw keeps the server's count; one it saw counts what it built"


def test_warnings_errors_and_failed_checks_mark_their_row(live):
    out = run(live, """
      play([{type:'stage', stage:'geometry_admission'},
            {type:'note', stage:'geometry_admission', text:'Input rejected', tone:'error'},
            {type:'stage', stage:'executor'},
            {type:'check', stage:'executor', statement:'Your geometry can be meshed by this engine', ok:false}]);
      Stage.final({pass:false, attempts:0, text:''});
      return read();""")
    assert marks(out)[0] == (ROWS[0], "bad") and marks(out)[3] == (ROWS[3], "bad"), marks(out)
    assert "Input rejected" in out["lanes"][0]["said"], "an error is said on its row, in view"
    assert "The check failed: Your geometry can be meshed by this engine" in out["lanes"][3]["said"]
    assert out["count"] == "", "a run with no attempt events invented a count"


def test_a_cancelled_run_stops_its_open_row_and_its_live_line(live):
    out = run(live, f"""
      play([{START}{{type:'attempt', stage:'builder', n:1, of:3}},
            {{type:'meshing', stage:'builder', engine:'snappy', budget_s:600}}]);
      const live = !document.querySelector('[data-row=build] .rr-live').hidden;
      Stage.final({{pass:false, cancelled:true, attempts:1, text:''}});
      return {{...read(), live, liveAfter: !document.querySelector('[data-row=build] .rr-live').hidden,
               barOpen: Stage.meshBar !== null}};""")
    assert out["live"], "the running row did not say what it was doing"
    assert (ROWS[2], "ended") in marks(out), marks(out)
    assert out["title"] == "Run cancelled" and "r-cancelled" in out["head"]
    assert not out["barOpen"] and not out["liveAfter"], "the live line kept going after the run ended"
    assert out["lanes"][3]["said"] == "Not reached."


def test_a_run_ends_once_however_many_polls_see_it_end(live):
    out = run(live, """
      play([{type:'stage', stage:'builder'}]);
      Stage.final({pass:false, attempts:1, text:'it failed'});
      Stage.final({pass:false, attempts:1, text:'it failed'});
      return {boxes: document.querySelectorAll('.proc').length,
              said: [...document.querySelectorAll('#cc .im.assistant .txt')].filter(t => t.textContent === 'it failed').length};""")
    assert out == {"boxes": 1, "said": 1}, out


def test_a_new_run_on_the_same_page_starts_clean(live):
    # The last run ended mid-mesh on its first attempt. The next run must start from attempt 1 with
    # its own four rows, and its rebuild must be set apart.
    out = run(live, """
      play([{type:'attempt', stage:'builder', n:1, of:3},
            {type:'meshing', stage:'builder', engine:'snappy', budget_s:600}]);
      Stage.mount();
      play([{type:'attempt', stage:'builder', n:1, of:3},
            {type:'meshing', stage:'builder', engine:'snappy', budget_s:600},
            {type:'meshed', stage:'builder', cells:10},
            {type:'attempt', stage:'builder', n:2, of:3}]);
      return {...read(), boxes: document.querySelectorAll('.proc').length};""")
    assert out["boxes"] == 1 and len(out["lanes"]) == 4
    assert out["attempts"] == ["Attempt 2"] * 3, out["attempts"]


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
