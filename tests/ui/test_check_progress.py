# Responsibility: Verify the geometry check's live line in a real browser, fed with the lines the
# application itself serves: the step running now with a moving mark and a counting time, the steps
# done folded under it, one quiet line once the check is done, a plain message with "Try again"
# when a step fails, a still mark while the check waits on the user or the server is quiet - and
# that it stays at the foot of the conversation only while it works.
# Boundaries: the console's own module in the page; the lines are application/geometry_progress's.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

T0 = 1_000_000.0
STEPS = {"reading": {"at": T0 + 2, "end": T0 + 6}, "measuring": {"at": T0 + 6, "end": T0 + 12},
         "openings": {"at": T0 + 12, "end": T0 + 14}, "pictures": {"at": T0 + 14, "end": T0 + 20}}


def _lines() -> dict:
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application import geometry_progress as gp

    limits = {"scout_limit_s": gc.SCOUT_SOFT_LIMIT_S, "naming_limit_s": gc.NAMING_SOFT_LIMIT_S}

    def rec(current, steps, **work):
        return {"scout": {"marker": T0, "picked_at": T0 + 2, "mode": "generic", "current": current,
                          "work": {"size_bytes": 180_000_000, "suffix": ".step", **work}, "steps": steps}}
    pending = {"status": "pending", "written_at": T0}
    reading = gp.progress_view(pending, None, rec("reading", {"reading": {"at": T0 + 2}}), T0 + 9, **limits)
    slow = gp.progress_view(pending, None, rec("reading", {"reading": {"at": T0 + 2}}), T0 + 400, **limits)
    scouted = {"status": "scouted", "written_at": T0 + 20, "seconds": 18.0}
    waiting = gp.progress_view(scouted, None, rec("pictures", STEPS), T0 + 60, **limits)
    naming_rec = rec("pictures", STEPS)
    naming_rec["naming"] = {"picked_at": T0 + 61, "current": "naming", "steps": {"naming": {"at": T0 + 62, "end": T0 + 68}}}
    done = gp.progress_view({"status": "ready", "written_at": T0 + 68, "seconds": 18.0}, {"requested_at": T0 + 60},
                            naming_rec, T0 + 90, **limits)
    failed = gp.progress_view({"status": "failed", "written_at": T0 + 30, "step": "scout", "retry": "scout",
                               "reason": "RuntimeError: the file has no solids to read"}, None,
                              rec("measuring", {"reading": {"at": T0 + 2, "end": T0 + 6}, "measuring": {"at": T0 + 6}},
                                  suffix=".igs"), T0 + 40, **limits)
    return {"reading": reading, "slow": slow, "waiting": waiting, "done": done, "failed": failed}


def _run(live, body: str):
    js = """(async () => {
      const m = await import('/static/js/render/check_progress.js');
      const { Stage } = await import('/static/js/render/stage.js');
      const L = __LINES__;
      const col = Stage.col();
      const line = m.createCheckProgress();
      const el = () => document.getElementById('check-progress');
      const snap = () => { const g = el().querySelector('.gp'); return {
        state: g.dataset.state, live: g.dataset.live === '1', now: g.querySelector('.gp-now').textContent,
        time: g.querySelector('.gp-time').textContent, note: g.querySelector('.gp-note')?.textContent || '',
        advice: g.querySelector('.gp-advice')?.textContent || '', reason: g.querySelector('.gp-reason')?.textContent || '',
        retry: !!g.querySelector('.gp-retry'), toggle: g.querySelector('.gp-toggle')?.textContent || '',
        open: !g.querySelector('.gp-steps').hidden, last: col.lastElementChild === el(),
        steps: [...g.querySelectorAll('.gp-step')].map(s => s.className.replace('gp-step ', '') + ':' + s.querySelector('.gp-label').textContent),
        role: g.querySelector('.gp-now').getAttribute('role') }; };
      __BODY__
    })()""".replace("__LINES__", json.dumps(_lines())).replace("__BODY__", body)
    return live.evaluate(js, timeout=60)


def test_the_running_step_is_named_with_a_moving_mark_and_its_time_counts_up(live):
    out = _run(live, """
      line.show(col, L.reading, {});
      const first = snap();
      await new Promise(r => setTimeout(r, 2200));
      const later = snap();
      line.show(col, L.slow, {});
      const slow = snap();
      return {first, later, slow, anim: getComputedStyle(el().querySelector('.gp-mark'), '::before').animationName};
    """)
    first, later, slow = out["first"], out["later"], out["slow"]
    assert first["state"] == "running" and first["live"] is True and first["role"] == "status"
    assert first["now"] == "Reading your CAD…" and first["time"] == "7 s"
    assert later["time"] in ("9 s", "10 s")                       # counted up between polls
    assert first["last"] is True and first["toggle"] == "1 done"    # queued, then picked up
    assert slow["note"] == "Still reading - big file (180 MB), this can take a few minutes"
    assert out["anim"] == "gp-pulse"
    assert_clean(live, "the live line while a step runs")


def test_waiting_on_the_answer_is_still_and_the_steps_done_unfold(live):
    out = _run(live, """
      line.show(col, L.waiting, {});
      const before = snap();
      el().querySelector('.gp-toggle').click();
      const opened = snap();
      Stage.chat('user', 'water through an elbow');
      line.show(col, L.waiting, {});
      return {before, opened, after: snap(), anim: getComputedStyle(el().querySelector('.gp-mark'), '::before').animationName};
    """)
    assert out["before"]["state"] == "waiting" and out["before"]["live"] is False and out["anim"] == "none"
    assert out["before"]["now"].startswith("Picture ready") and out["before"]["time"] == ""
    assert out["before"]["toggle"].startswith("5 done") and out["before"]["open"] is False
    assert out["opened"]["open"] is True
    assert out["opened"]["steps"] == ["done:Queued", "done:Read your CAD", "done:Measured the shape",
                                      "done:Found the openings", "done:Drew the pictures"]
    assert out["after"]["last"] is True and out["after"]["open"] is True   # moved below the answer, still open


def test_a_done_check_is_one_quiet_line_that_stays_where_it_ended(live):
    out = _run(live, """
      line.show(col, L.reading, {});
      Stage.chat('user', 'water through an elbow');
      line.show(col, L.done, {});              // it ended after the answer: it ends below it
      const done = snap();
      Stage.chat('assistant', 'Which fluid is it?');
      line.show(col, L.done, {});
      return {done, after: snap(), state: line.state()};
    """)
    assert out["done"]["state"] == "done" and out["done"]["live"] is False
    assert out["done"]["now"] == "Checked in 24 s" and out["done"]["toggle"].startswith("Details")
    assert out["done"]["last"] is True
    assert out["after"]["last"] is False                       # the conversation goes on below it
    assert out["state"]["live"] is False


def test_a_failed_step_says_what_happened_and_what_to_do_with_a_try_again(live):
    out = _run(live, """
      let asked = 0;
      line.show(col, L.failed, {retry: () => { asked += 1; return new Promise(() => {}); }});
      const failed = snap();
      el().querySelector('.gp-retry').click();
      return {failed, asked, label: el().querySelector('.gp-retry').textContent,
              disabled: el().querySelector('.gp-retry').disabled};
    """)
    f = out["failed"]
    assert f["state"] == "failed" and f["live"] is False and f["now"] == "Couldn't finish measuring the shape."
    assert f["reason"] == "The file has no solids to read." and "export the part as STEP" in f["advice"]
    assert f["retry"] is True and out["asked"] == 1 and out["disabled"] is True and out["label"] == "Starting again…"


def test_a_quiet_server_stops_the_count_and_says_so_and_a_cleared_conversation_stays_cleared(live):
    out = _run(live, """
      line.show(col, L.reading, {});
      line.show(col, null, {offline: true});
      const offline = snap();
      line.show(col, null, {ended: true});
      const ended = snap();
      Stage.mount();                                  // a run started: the conversation is new
      line.show(Stage.col(), L.reading, {});
      return {offline, ended, gone: !document.getElementById('check-progress')};
    """)
    assert out["offline"]["live"] is False and "Can't reach the server" in out["offline"]["note"]
    assert out["ended"]["live"] is False and "Reload the page" in out["ended"]["note"]
    assert out["gone"] is True
