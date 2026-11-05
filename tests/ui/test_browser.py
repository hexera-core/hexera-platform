# Responsibility: Verify the shipped page boots in a real browser and cannot be made to render delivered text as markup.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

#: Payloads that have each, at some point in the industry, turned delivered text into markup.
#: One corpus, run through every path that writes backend text into the document.
HOSTILE = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "</div><iframe src=javascript:alert(1)></iframe>",
    '"><svg/onload=alert(1)>',
    "<a href='javascript:alert(1)'>click</a>",
    "`` **bold** <b onmouseover=alert(1)>md</b>",
]


def test_the_live_application_boots_and_can_be_driven(live):
    assert_clean(live, "the live application")

    # ONE entrypoint, executed once. A second copy of the application in the same document
    # would double every listener and every timer.
    entry = [u for u in live.requests if u.endswith("/static/js/main.js")]
    assert len(entry) == 1, f"the entrypoint was fetched {len(entry)} times: {entry}"

    # The composer explains itself before it will accept anything: geometry first.
    composer = live.evaluate("""(() => {
      const chat = document.getElementById('chat-input');
      return {
        uploadOffered: !document.getElementById('upload-btn').disabled,
        accepts: document.getElementById('step-file-input').getAttribute('accept') || '',
        chatDisabled: chat.disabled,
        explains: (chat.placeholder || '').length > 10,
      };
    })()""")
    assert composer["uploadOffered"], "there is no way to give the application a geometry"
    assert "stl" in composer["accepts"].lower() and "step" in composer["accepts"].lower()
    assert composer["chatDisabled"] and composer["explains"], \
        "the composer is inert without saying why"

    # Enter sends. Proved by the request leaving the browser, not by a stub.
    live.evaluate("""(async () => {
      (await import('/static/js/core/state.js')).set.sessionId('browser-smoke-session');
      const c = await import('/static/js/shell/composer.js');
      c.enableInput();
      document.getElementById('chat-input').value = 'mesh this for me';
    })()""")
    live.press("Enter", vk=13, text="\r")
    live.wait_for("true")
    live.settle(0.5)
    assert any("/chat/message" in u for u in live.requests), \
        "pressing Enter in the composer sent nothing"

    # Credentials mint a ticket over authenticated HTTPS; only the ticket and the replay cursor
    # may travel in a URL, which every proxy on the path is entitled to log.
    ws = live.evaluate("""(async () => {
      const client = await import('/static/js/api/client.js');
      client.useIdentity('someone');
      const url = (await import('/static/js/api/endpoints.js'))
        .streamUrl('job-1', 'TICKET-xyz', 41);
      return {url, secure: location.protocol === 'https:'};
    })()""")
    assert ws["url"].startswith("wss://" if ws["secure"] else "ws://"), ws["url"]
    assert "ticket=TICKET-xyz" in ws["url"] and "since=41" in ws["url"]
    # ONLY the ticket and the cursor may travel in a URL. The identity in play must not, and no
    # credential parameter may appear at all - the page no longer holds a key or a signature.
    for leaked in ("someone", "api_key", "x-api-key", "sig", "key="):
        assert leaked.lower() not in ws["url"].lower(), f"{leaked} reached the socket URL"

    # The mesh viewer, opened for real. Nothing else in this suite loads the vendored vtk.js
    # bundle - it is fetched lazily, the first time a user opens a mesh - so without this a
    # release could stop shipping it unnoticed. This job does not exist, so the surface request
    # legitimately 404s, which also exercises the failure path: a misplaced copy of the
    # delivered-mesh figures used to throw out of that handler before it could write anything,
    # leaving a viewer that explained nothing. Provoked last, deliberately.
    live.evaluate("""(async () => {
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('no-such-job', document.getElementById('stage'), {});
      document.getElementById('viewer-no-such-job').scrollIntoView();
    })()""", timeout=60)
    live.wait_for("typeof window.vtk !== 'undefined'", timeout=60,
                  what="the vendored viewer bundle to load")
    assert any(u.endswith("/static/vendor/vtk.js") for u in live.requests), \
        "the viewer never asked for its rendering bundle"
    live.wait_for("(document.getElementById('v-status-no-such-job')||{}).textContent"
                  " && document.getElementById('v-status-no-such-job')"
                  ".textContent.trim() !== 'loading…'",
                  timeout=60, what="the viewer to explain why it could not load")


def test_delivered_text_cannot_become_markup_in_the_document(live):
    result = live.evaluate("""(async () => {
      const { dispatch } = await import('/static/js/core/events.js');
      const { Stage } = await import('/static/js/render/stage.js');
      window.__fired = 0;
      window.alert = () => { window.__fired++; };
      Stage.mount();
      const payloads = __PAYLOADS__;
      let cursor = null;
      payloads.forEach((p, i) => {
        cursor = dispatch({type:'stage', stage:'builder'}, Stage, cursor);
        for (const ev of [
          {type:'check', stage:'builder', statement:p, ok:true},
          {type:'note', stage:'builder', text:p, tone:'warn'},
          {type:'file', stage:'builder', display_path:p, byte_count:12, operation:'created'},
          {type:'tool_call', stage:'builder', id:'c'+i, tool_name:p, arguments:{path:p}},
          {type:'tool_result', stage:'builder', tool_call_id:'c'+i, status:'success', output:p},
          {type:'rationale', stage:'builder', conclusion:p, because:p},
          {type:'reasoning', stage:'builder', id:'r'+i, phase:'completed', content:p},
        ]) cursor = dispatch(ev, Stage, cursor);
        Stage.chat('assistant', p);          // the markdown path
        Stage.chat('user', p, 'You');
      });
      await new Promise(r => setTimeout(r, 250));   // let any onerror/onload fire
      const stage = document.getElementById('stage');
      const inline = [...stage.querySelectorAll('*')]
        .filter(el => [...el.attributes].some(a => a.name.toLowerCase().startsWith('on')))
        .map(el => el.tagName + '[' + [...el.attributes].map(a => a.name).join(',') + ']');
      return {
        fired: window.__fired,
        injected: stage.querySelectorAll('script, iframe, object, embed').length,
        javascriptHrefs: stage.querySelectorAll('[href^="javascript:"]').length,
        inline,
        text: stage.textContent,
      };
    })()""".replace("__PAYLOADS__", json.dumps(HOSTILE)))

    # `svg` is not in that list on purpose: the timeline draws its own SVG icons, so their
    # presence proves nothing. A payload-built one (`"><svg/onload=...`) is caught by the
    # inline-handler scan below, which is the property that actually matters.
    assert result["fired"] == 0, "a payload executed"
    assert result["injected"] == 0, "a payload became an element"
    assert result["javascriptHrefs"] == 0, "a payload became a javascript: link"
    assert result["inline"] == [], f"a payload became an inline handler: {result['inline']}"
    for payload in HOSTILE:
        assert payload in result["text"], \
            f"{payload!r} was neither escaped into view nor rendered - it vanished"
    assert_clean(live, "the timeline under hostile payloads")
