# Responsibility: Verify a page carries one run at a time, and offers a mesh only on proof that a gate passed.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_page_carries_one_run_at_a_time_through_its_whole_life(live):
    out = live.evaluate("""(async () => {
      const S = await import('/static/js/core/state.js');
      const seen = {};
      const read = () => ({jobId: S.get.jobId(), status: S.get.jobStatus(),
                           outcome: S.get.outcomeMessage(), lane: S.get.laneCursor(),
                           session: S.get.sessionId(), terminal: S.isTerminal()});

      S.set.sessionId('sess-1');                    // the upload belongs to the PAGE
      S.beginRun('job-1');
      seen.idle = read();

      S.set.jobStatus('running'); S.set.laneCursor('builder');
      seen.running = read();

      // a reload mid-run: the log is replayed, the closing text is held for the result card
      S.set.laneCursor('reviewer'); S.set.outcomeMessage('your mesh is ready');
      seen.replaying = read();

      S.set.jobStatus('succeeded');
      seen.terminal = read();

      S.beginRun('job-2');                          // a re-review, same page
      seen.rereview = read();

      seen.failedIsTerminal = (S.set.jobStatus('failed'), S.isTerminal());
      seen.pendingIsNot = (S.set.jobStatus('pending'), S.isTerminal());
      return seen;
    })()""")

    assert out["idle"]["terminal"] is False and out["running"]["terminal"] is False
    assert out["terminal"]["terminal"] is True, "a finished run did not read as finished"
    assert out["failedIsTerminal"] is True and out["pendingIsNot"] is False, \
        "the single definition of 'the run is over' disagrees with itself"

    fresh = out["rereview"]
    assert fresh["jobId"] == "job-2"
    assert fresh["status"] == "" and fresh["outcome"] == "" and fresh["lane"] is None, \
        f"the new run inherited the finished one's state: {fresh}"
    assert fresh["session"] == "sess-1", "the upload session was discarded with the run"


def test_a_finished_job_becomes_the_same_result_card_however_it_was_reached(live):
    out = live.evaluate("""(async () => {
      const { terminalResult } = await import('/static/js/realtime/stream.js');
      return {
        won: terminalResult({
          status:'succeeded', current_attempt:2, mesh_available:true,
          reviewer_verdict:'PASS', reviewer_findings:['a'], reviewer_reasoning:'why',
          artifacts:[{artifact_type:'mesh_bundle', label:'OpenFOAM case',
                      size_bytes:12, download_url:'/d'}],
        }, 'your mesh is ready'),
        lost: terminalResult({status:'failed'}, ''),
      };
    })()""")

    won = out["won"]
    assert won["pass"] is True and won["attempts"] == 2 and won["text"] == "your mesh is ready"
    assert won["files"] == [{"type": "mesh_bundle", "label": "OpenFOAM case",
                             "size": 12, "url": "/d"}]
    assert won["meshAvailable"] is True and won["verdict"] == "PASS"

    lost = out["lost"]
    assert lost["pass"] is False and lost["attempts"] == 0
    assert lost["files"] == [] and lost["verdict"] == "" and lost["meshAvailable"] is False


#: Every way a run can end, and the surface it earns. `unreviewed` is the only case in which a
#: mesh that did not pass may still be shown: the retry loop gave up, but a mesh reached the
#: REVIEWER and got a verdict, so it cleared every executor VALIDITY gate and only the quality
#: bar was missed.
SURFACES = [
    ("{pass:true, attempts:1}", "mesh", "it passed"),
    ('{pass:false, verdict:"FAIL", meshAvailable:true}', "unreviewed",
     "a real verdict proves a real mesh reached the reviewer"),
    ("{pass:false, meshAvailable:true}", "none",
     "mesh_available alone - no reviewer ever saw it, so no gate was proved"),
    ('{pass:false, verdict:"FAIL", meshAvailable:false}', "none", "a verdict with no mesh"),
    ("{pass:false}", "none", "a terminal failure"),
    ("null", "none", "no result at all"),
]


def test_a_mesh_is_only_offered_on_proof_that_a_gate_passed(live):
    rows = live.evaluate("""(async () => {
      const { resultSurface } = await import('/static/js/core/events.js');
      return [__CASES__].map(([data]) => resultSurface(data));
    })()""".replace("__CASES__", ", ".join(f"[{data}]" for data, _, _ in SURFACES)))

    wrong = [f"{why}: expected {expected!r}, got {got!r}"
             for (_, expected, why), got in zip(SURFACES, rows, strict=True)
             if got != expected]
    assert not wrong, "\n".join(wrong)
