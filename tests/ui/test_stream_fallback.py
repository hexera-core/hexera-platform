# Responsibility: Verify a run page whose live stream cannot open says so, keeps polling, and fills
# the steps in when the run ends - instead of a bare "Generating mesh" that looks stuck.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

JOB = "0f760f95-1111-4222-8333-444455556666"


def test_a_stream_that_cannot_open_is_said_and_the_steps_arrive_at_the_end(live):
    out = live.evaluate("""(async () => {
      const S = await import('/static/js/core/state.js');
      const R = await import('/static/js/realtime/stream.js');
      const { Stage } = await import('/static/js/render/stage.js');
      const job = '__JOB__';
      let status = 'running', socketsOpen = false;
      const realFetch = window.fetch;
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (u.includes('/api/v1/ws/ticket')) return new Response(JSON.stringify({ticket: 't'}),
          {status: 200, headers: {'content-type': 'application/json'}});
        if (u.includes('/api/v1/simulation/' + job)) return new Response(JSON.stringify({
          id: job, status, created_at: new Date().toISOString(), started_at: 'x',
          ended_at: status === 'running' ? null : new Date().toISOString(), artifacts: []}),
          {status: 200, headers: {'content-type': 'application/json'}});
        return realFetch(url, opts);
      };
      // a socket that never opens while the run is live (what Next did to every upgrade), and
      // one that replays the finished log once it can
      window.WebSocket = class {
        constructor(u) { this.url = u; setTimeout(() => {
          if (!socketsOpen) { this.onclose && this.onclose({}); return; }
          this.onopen && this.onopen();
          this.onmessage && this.onmessage({data: JSON.stringify(
            {type: 'closing', seq: 4, text: 'Your mesh is ready.'})});
        }, 5); }
        close() {}
      };
      const notice = () => (document.getElementById('notice').textContent || '');
      const sub = () => (document.getElementById('proc-sub') || {}).textContent || '';
      Stage.mount(); S.beginRun(job); Stage.ensureProc();
      R.start(0);
      await new Promise(r => setTimeout(r, 5800));      // three failed attempts and a status poll
      const during = {notice: notice(), sub: sub()};
      status = 'succeeded'; socketsOpen = true;
      await new Promise(r => setTimeout(r, 6000));      // the next poll sees the end
      return {during, after: {notice: notice(), outcome: S.get.outcomeMessage()}};
    })()""".replace("__JOB__", JOB))
    assert "Live steps are not reaching this page" in out["during"]["notice"], out
    assert "live steps unavailable" in out["during"]["sub"], out
    assert "Live steps" not in out["after"]["notice"], out
    # the catch-up replay read the finished log, closing line and all
    assert out["after"]["outcome"] == "Your mesh is ready.", out
