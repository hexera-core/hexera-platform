# Responsibility: Verify a page carries one run at a time, and offers a mesh only on proof that a gate passed.
from __future__ import annotations

import json

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


def test_a_run_remembers_the_conversation_it_came_from(live):
    # "Run again" goes to the conversation that started the run - never to whatever session the
    # page happens to hold when it ends. A run opened by link has none, even after an upload.
    out = live.evaluate("""(async () => {
      const S = await import('/static/js/core/state.js');
      const before = S.get.sessionId();
      S.set.sessionId(null);
      S.beginRun('job-link');                                  // opened from a link
      const linked = S.get.runSessionId();
      S.set.sessionId('sess-other');                           // a later upload, other geometry
      const afterUpload = S.get.runSessionId();
      S.beginRun('job-chat', {sessionId: S.get.sessionId()});  // a run this conversation started
      const chat = S.get.runSessionId();
      S.beginRun('job-link-2');
      const cleared = S.get.runSessionId();
      // every run the page began keeps its origin, so a re-review of an earlier one can take it
      const origins = {link: S.get.runOrigin('job-link'), chat: S.get.runOrigin('job-chat'),
                       unknown: S.get.runOrigin('job-never-seen')};
      S.set.sessionId(before);
      return {linked, afterUpload, chat, cleared, origins};
    })()""")
    assert out == {"linked": None, "afterUpload": None, "chat": "sess-other", "cleared": None,
                   "origins": {"link": None, "chat": "sess-other", "unknown": None}}, out


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
#: mesh that did not pass may still be shown, and it needs TWO things: something DELIVERED, and
#: something that proves a validity gate passed. Either proof will do - a real reviewer verdict,
#: or the executor's own account of its gates - and neither is ever assumed from the other.
SURFACES = [
    ("{pass:true, attempts:1}", "mesh", "it passed"),
    ('{pass:false, verdict:"FAIL", meshAvailable:true}', "unreviewed",
     "a real verdict proves a real mesh reached the reviewer"),
    ("{pass:false, meshAvailable:true, executorValidated:true}", "unreviewed",
     "no verdict, but every executor gate passed and the mesh was delivered - the run whose "
     "review never returned a judgement"),
    ("{pass:false, meshAvailable:true}", "none",
     "mesh_available alone - no reviewer ever saw it, so no gate was proved"),
    ("{pass:false, executorValidated:true}", "none",
     "every gate passed, but nothing was delivered to show"),
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


#: What job 0ff6f9e5 actually holds, after its mesh was re-delivered from its own workspace: a
#: record that has been re-rendered and knows the mesh is stored, beside a `closing` event still
#: carrying the sentence written at the moment the run ended.
RECORD_NOW = "Mesh generation completed; the run did not.\nYour mesh was built and it is stored."
SAID_AT_THE_TIME = ("No mesh this time.\nNo downloadable mesh deliverable is available.\n"
                    "You can run it again with the same request - nothing needs to change.")


def test_the_result_card_reads_the_record_not_the_replayed_closing_event(live):
    """A finished run keeps two copies of its closing words and they can disagree.

    final_message is re-rendered from the terminal record on every read, so it is what is true
    NOW. The `closing` event is what we SAID when the run ended, and it is replayed verbatim out
    of the log. They diverge the moment anything lands after the ending - which is exactly what a
    re-delivery is. Job 0ff6f9e5's event still reads "No downloadable mesh deliverable is
    available" while its record says the mesh is stored and the panel beside it offers three
    downloads.
    """
    got = live.evaluate("""(async () => {
      const { terminalResult } = await import('/static/js/realtime/stream.js');
      const job = {status: 'failed', current_attempt: 1, mesh_available: true,
                   artifacts: [], final_message: __RECORD__};
      const withRecord = terminalResult(job, __SAID__);
      // and a run whose record carries no message at all still shows what was said
      const {final_message, ...noRecord} = job;
      return {withRecord: withRecord.text, withoutRecord: terminalResult(noRecord, __SAID__).text};
    })()""".replace("__RECORD__", json.dumps(RECORD_NOW)).replace("__SAID__",
                                                                 json.dumps(SAID_AT_THE_TIME)))

    assert got["withRecord"] == RECORD_NOW, (
        "the result card showed the replayed closing event instead of the re-rendered record, so "
        "a mesh that was delivered after the run ended is still announced as missing")
    # THE TRANSCRIPT IS NOT REWRITTEN. The event is still what was said; it is simply not what the
    # card is for. A record with nothing in it falls back to it rather than showing nothing.
    assert got["withoutRecord"] == SAID_AT_THE_TIME
