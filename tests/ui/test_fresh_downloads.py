# Responsibility: Verify a Download click asks for a fresh link at click time, and fetches a new
#                 one and tries once more when the store refuses the download.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_JOB = "aaaabbbb-2222-4222-b222-aaaaaaaaaaaa"
_DECK = "cccccccc-3333-4333-b333-cccccccccccc"


def test_a_click_downloads_from_a_link_minted_at_click_time(live):
    out = live.evaluate("""(async () => {
      const { downloadFresh } = await import('/static/js/render/download.js');
      const asked = [], started = [];
      const fetchLink = async (job, id) => {
        asked.push([job, id]);
        return { download_url: 'https://store/fresh-' + asked.length, expires_in_seconds: 7200 };
      };
      const start = (url, opts) => { started.push(url); };
      const r = await downloadFresh('__JOB__', '__DECK__', { fetchLink, start });
      return { asked, started, startedOk: r.started, retried: r.retried };
    })()""".replace("__JOB__", _JOB).replace("__DECK__", _DECK))
    assert out["asked"] == [[_JOB, _DECK]]
    assert out["started"] == ["https://store/fresh-1"], "the download did not use the fresh link"
    assert out["startedOk"] is True and out["retried"] is False


def test_a_refused_download_gets_a_new_link_and_one_more_try(live):
    out = live.evaluate("""(async () => {
      const { downloadFresh } = await import('/static/js/render/download.js');
      let n = 0;
      const fetchLink = async () => ({ download_url: 'https://store/link-' + (++n) });
      const started = [], refusals = [];
      const start = (url, opts) => { started.push(url); refusals.push(opts && opts.onRefused); };
      const r = await downloadFresh('j', 'a', { fetchLink, start });
      // the store refuses the first link (expired, or a dropped connection's retry)
      await refusals[0]();
      // and refuses again: there is no third try, the user is not looped
      if (refusals[1]) await refusals[1]();
      await refusals[0]();
      return { started, retried: r.retried, asks: n };
    })()""")
    assert out["started"] == ["https://store/link-1", "https://store/link-2"], out
    assert out["retried"] is True and out["asks"] == 2


def test_a_failed_ask_for_a_link_is_asked_once_more(live):
    out = live.evaluate("""(async () => {
      const { downloadFresh } = await import('/static/js/render/download.js');
      let n = 0;
      const fetchLink = async () => {
        n += 1;
        if (n === 1) throw new TypeError('Failed to fetch');
        return { download_url: 'https://store/second-ask' };
      };
      const started = [];
      await downloadFresh('j', 'a', { fetchLink, start: (u) => started.push(u) });
      return { started, n };
    })()""")
    assert out == {"started": ["https://store/second-ask"], "n": 2}


def test_no_link_at_all_falls_back_to_the_link_the_page_loaded_with(live):
    out = live.evaluate("""(async () => {
      const { downloadFresh } = await import('/static/js/render/download.js');
      let fellBack = 0, n = 0;
      const fetchLink = async () => { n += 1; throw new TypeError('offline'); };
      const r = await downloadFresh('j', 'a', {
        fetchLink, start: () => { throw new Error('nothing to start'); },
        fallback: () => { fellBack += 1; } });
      // a refusal that will not change is not asked twice
      let m = 0;
      const gone = async () => { m += 1; const e = new Error('gone'); e.status = 404; throw e; };
      await downloadFresh('j', 'a', { fetchLink: gone, fallback: () => {} });
      return { started: r.started, fellBack, n, m };
    })()""")
    assert out == {"started": False, "fellBack": 1, "n": 2, "m": 1}


def test_the_result_panels_buttons_ask_for_a_fresh_link_when_clicked(live):
    # Both panels with deliverables - the mesh viewer's and the repair panel - carry the artifact
    # id, and a click is taken over rather than following the href signed at page load.
    out = live.evaluate("""(async () => {
      const { openRepairPanel } = await import('/static/js/render/repair.js');
      const { bindFreshDownloads, downloadAttrs } = await import('/static/js/render/download.js');
      const anchor = document.createElement('div');
      document.body.appendChild(anchor);
      openRepairPanel('__JOB__', anchor, { files: [
        { id: '__DECK__', type: 'repaired_cad', label: 'Repaired geometry', size: 10,
          url: 'https://store/stale' }] });
      const a = document.querySelector('#repair-__JOB__ a[download]');
      const attrs = { job: a.dataset.job, artifact: a.dataset.artifactId,
                      href: a.getAttribute('href') };
      // the binding itself, with the network replaced: a click asks, and does not navigate
      const panel = document.createElement('div');
      panel.innerHTML = `<a class="v-dlbtn" href="https://store/stale"${downloadAttrs('__JOB__',
        { id: '__DECK__' })} download><b>Deck</b></a>`;
      document.body.appendChild(panel);
      const asked = [], started = [];
      bindFreshDownloads(panel, {
        fetchLink: async (job, id) => { asked.push([job, id]); return { download_url: 'https://store/new' }; },
        start: (u) => started.push(u) });
      bindFreshDownloads(panel, {});   // bound twice: still one download per click
      const ev = new MouseEvent('click', { bubbles: true, cancelable: true });
      panel.querySelector('b').dispatchEvent(ev);
      await new Promise((r) => setTimeout(r, 0));
      const legacy = downloadAttrs('j', { url: 'x' });   // an older API row with no id
      anchor.remove(); panel.remove();
      return { attrs, asked, started, prevented: ev.defaultPrevented, legacy };
    })()""".replace("__JOB__", _JOB).replace("__DECK__", _DECK))
    assert out["attrs"] == {"job": _JOB, "artifact": _DECK, "href": "https://store/stale"}
    assert out["asked"] == [[_JOB, _DECK]] and out["started"] == ["https://store/new"]
    assert out["prevented"] is True, "the click followed the stale href"
    assert out["legacy"] == "", "a row without an id must keep following its href"


def test_the_viewer_lists_each_deliverable_with_its_id():
    # The viewer's download row is built from terminalResult's files; both must carry the id.
    import pathlib
    stream = pathlib.Path("ui/js/realtime/stream.js").read_text()
    assert "id: a.id" in stream, "terminalResult drops the artifact id the click needs"
    viewer = pathlib.Path("ui/js/viewer/viewer.js").read_text()
    assert "downloadAttrs(job,f)" in viewer and "bindFreshDownloads(box)" in viewer


def test_a_refusal_page_in_the_frame_is_noticed_and_a_blank_frame_is_not(live):
    out = live.evaluate("""(async () => {
      const { startDownload } = await import('/static/js/render/download.js');
      let refused = 0;
      // a page loading in the frame is what a refused link looks like (the store's error page);
      // a same-origin page stands in for it here
      const frame = startDownload('/readyz', { onRefused: () => { refused += 1; } });
      await new Promise((r) => { frame.addEventListener('load', r); setTimeout(r, 5000); });
      await new Promise((r) => setTimeout(r, 50));
      const hidden = frame.hidden;
      frame.remove();
      return { refused, hidden };
    })()""")
    assert out == {"refused": 1, "hidden": True}
