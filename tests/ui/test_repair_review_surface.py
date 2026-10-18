# Responsibility: Verify the engineer's review of a parked repair run in a real browser - the
#                 first group's sample one defect per screen with the part pinned and framed,
#                 apply-all only after the sample and never for paired defects, a redirect played
#                 back before the engineer moves on, "Run it" posting and saying what stays
#                 unanswered, and proposal-less defects offering only leave and redirect.
# Boundaries: the skin is the one the worker stores; only the network is stood in for.
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

JOB = "11111111-1111-1111-1111-111111111111"
SESSION = "review-session"


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (1.0, 0.5, 0.5))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _item(n: int, group: str, code: str, *, proposal=True, pairing="", size=1.0, at=None) -> dict:
    return {
        "id": f"item-{n}", "group_key": group, "defect_code": code, "severity": "warn",
        "entity_type": "face", "entity_index": n, "fingerprint": f"fp-{n}",
        "measurements": {"gap_mm": size}, "message": f"defect {n} message", "origin": "check",
        "pairing_key": pairing, "state": "open", "decision": None,
        "proposal": ({"op": "sew", "targets": [f"fp-{n}"], "rationale": f"sew {n} shut",
                      "provenance": "rules"} if proposal else None),
        "camera": {"centroid_mm": at or [500.0, 250.0, 250.0], "normal": [0, 0, 1],
                   "extent_mm": 100.0}}


def _review(status: str = "open") -> dict:
    gap = [_item(1, "gap", "GAP", size=1.0, at=[100.0, 250.0, 500.0]),
           _item(2, "gap", "GAP", size=9.0, at=[200.0, 250.0, 500.0]),
           _item(3, "gap", "GAP", size=5.0), _item(4, "gap", "GAP", size=7.0)]
    pair = [_item(5, "seam", "SEAM", pairing="p1"), _item(6, "seam", "SEAM", pairing="p1"),
            _item(7, "seam", "SEAM", pairing="p2")]
    bare = [_item(8, "odd", "ODD", proposal=False)]

    def group(key, items, op, allowed, sample):
        return {"group_key": key, "defect_code": items[0]["defect_code"], "proposed_op": op,
                "count": len(items), "sampled_item_ids": [i["id"] for i in items[:sample]],
                "apply_all_allowed": allowed, "items": items}
    return {"job_id": JOB, "status": status, "geometry_session_id": SESSION,
            "groups": [group("gap", gap, "sew", True, 2), group("seam", pair, "sew", False, 2),
                       group("odd", bare, "", True, 1)], "decided": 0, "open": 8}


PRELUDE = """
const REVIEW = __REVIEW__, SKIN = __SKIN__;
window.__calls = [];
window.__script = {redirect: {kind: 'directive', playback: 'i will leave the fillet alone'},
                   run: {status: 202, body: {job_id: 'j', status: 'pending', directives: 1}}};
const real = window.fetch.bind(window);
const json = (b, status = 200) => new Response(JSON.stringify(b),
  {status, headers: {'Content-Type': 'application/json'}});
window.fetch = (u, o) => {
  const s = String(u), m = (o && o.method) || 'GET';
  if (s.includes('/check/skin')) return Promise.resolve(json(SKIN));
  if (s.endsWith('/review/decision')) {
    if (window.__script.reject) return Promise.reject(new TypeError('network down'));
    const body = JSON.parse(o.body);
    window.__calls.push({url: s, method: m, body});
    if (body.action === 'redirect') return Promise.resolve(json(window.__script.redirect));
    if (body.action === 'apply_all') return Promise.resolve(json({kind: 'applied', changed: 2}));
    return Promise.resolve(json({kind: body.action === 'accept' ? 'accepted' : 'left'}));
  }
  if (s.endsWith('/review/run')) {
    window.__calls.push({url: s, method: m, body: null});
    return Promise.resolve(json(window.__script.run.body, window.__script.run.status));
  }
  if (s.endsWith('/review')) return Promise.resolve(json(REVIEW));
  return real(u, o);
};
"""


def _prelude(review: dict | None = None) -> str:
    return (PRELUDE.replace("__REVIEW__", json.dumps(review or _review()))
            .replace("__SKIN__", json.dumps(_skin())))


def _open(live, review=None, extra: str = "") -> None:
    live.evaluate("(async () => {" + _prelude(review) + """
      const { openReview } = await import('/static/js/review/review.js');
      window.__done = [];
      window.__rv = await openReview('""" + JOB + """', {onRun: (r) => window.__done.push(r)});
      """ + extra + "})()")
    live.wait_for("!!document.querySelector('.rv .rv-item')", what="the first defect screen")


def _text(live, sel: str) -> str:
    return live.evaluate(f"(document.querySelector({json.dumps(sel)})||{{}}).textContent||''")


def _click(live, sel: str) -> None:
    live.evaluate(f"document.querySelector({json.dumps(sel)}).click()")
    live.settle(0.1)


def test_a_parked_run_opens_review_with_the_first_groups_sample(live):
    _open(live)
    out = live.evaluate("""({
      rail: [...document.querySelectorAll('.rv-rail li')].map(li => [li.dataset.group, li.className]),
      item: document.querySelector('.rv-item').textContent,
      full: getComputedStyle(document.querySelector('.rv')).position,
      count: document.querySelector('.rv-step').textContent,
    })""")
    assert [r[0] for r in out["rail"]] == ["gap", "seam", "odd"]
    assert "current" in out["rail"][0][1] and "todo" in out["rail"][1][1]
    assert out["full"] == "fixed", "the review is not full screen"
    # one defect at a time: the first of the sample, never the fourth
    assert "defect 1 message" in out["item"] and "defect 4 message" not in out["item"]
    assert "1 of 2" in out["count"], out["count"]
    # the part is pinned and the camera is on the defect, not on the whole part
    live.wait_for("window._vdbg && !!window._vdbg['review:" + JOB + "']", what="the viewport")
    view = live.evaluate("""(() => { const v = window._vdbg['review:__J__'];
      return {pin: v.pin(), focal: v.focal(), visible: v.visible()}; })()""".replace("__J__", JOB))
    assert view["visible"] == 1, "the defect's pin is not on screen"
    for got, want in zip(view["focal"], [0.1, 0.25, 0.5], strict=True):
        assert abs(got - want) < 1e-3, f"the camera is not on the defect: {view['focal']}"
    _click(live, ".rv-leave")
    assert "defect 2 message" in _text(live, ".rv-item")
    live.evaluate("document.querySelector('.rv-do').click()")
    live.settle(0.2)
    assert [(c["body"].get("item_id"), c["body"]["action"])
            for c in live.evaluate("window.__calls")] == [("item-1", "leave"), ("item-2", "accept")]


def test_apply_all_appears_only_after_the_sample_and_never_for_paired_groups(live):
    _open(live)
    assert live.evaluate("!document.querySelector('.rv-fixall')"), "apply-all shown mid-sample"
    _click(live, ".rv-do")
    assert live.evaluate("!document.querySelector('.rv-fixall')"), "apply-all shown mid-sample"
    _click(live, ".rv-do")
    # the sample is done: fix the rest the same way, or keep going, or the largest first
    assert "Fix all 2 the same way" in _text(live, ".rv-fixall")
    assert live.evaluate("!!document.querySelector('.rv-onebyone') && "
                         "!!document.querySelector('.rv-largest')")
    # largest first reorders what is left by measurement, biggest at the top
    _click(live, ".rv-largest")
    assert "defect 4 message" in _text(live, ".rv-item"), _text(live, ".rv-item")
    _click(live, ".rv-leave")
    assert "defect 3 message" in _text(live, ".rv-item")
    _click(live, ".rv-leave")
    # the paired group: sample shown, then NO fix-all, and the screen says why
    assert "defect 5 message" in _text(live, ".rv-item")
    _click(live, ".rv-leave")
    _click(live, ".rv-leave")
    assert live.evaluate("!document.querySelector('.rv-fixall')"), "paired defects offered apply-all"
    why = _text(live, ".rv-why").lower()
    assert "pair" in why and "one" in why, why
    assert live.evaluate("!!document.querySelector('.rv-onebyone')")


def test_fix_all_posts_the_group_and_moves_on(live):
    _open(live)
    _click(live, ".rv-do")
    _click(live, ".rv-do")
    _click(live, ".rv-fixall")
    calls = live.evaluate("window.__calls")
    assert calls[-1]["body"] == {"group_key": "gap", "action": "apply_all"}
    assert "defect 5 message" in _text(live, ".rv-item")
    assert live.evaluate("document.querySelector('.rv-rail li').className").count("done")


def test_a_redirect_plays_back_before_it_records(live):
    _open(live)
    live.evaluate("""document.querySelector('.rv-redirect-text').value = 'leave the fillet alone';""")
    _click(live, ".rv-redirect-go")
    # the interpretation is said back and the engineer has not been moved on from the defect
    assert "i will leave the fillet alone" in _text(live, ".rv-playback")
    assert "defect 1 message" in _text(live, ".rv-item")
    call = live.evaluate("window.__calls")[-1]["body"]
    assert call == {"item_id": "item-1", "action": "redirect", "text": "leave the fillet alone"}
    # the API records a directive when it is interpreted, so the screen says so and offers the way
    # back: "change it" reopens the box, "that's right" moves on
    assert "recorded" in _text(live, ".rv-playback").lower()
    _click(live, ".rv-change")
    assert live.evaluate("!document.querySelector('.rv-playback') && "
                         "!!document.querySelector('.rv-redirect-text')")
    _click(live, ".rv-redirect-go")           # nothing typed: nothing sent
    assert len(live.evaluate("window.__calls")) == 1
    live.evaluate("""document.querySelector('.rv-redirect-text').value = 'again';""")
    _click(live, ".rv-redirect-go")
    _click(live, ".rv-confirm")
    assert "defect 2 message" in _text(live, ".rv-item")


def test_a_refused_redirect_says_why_and_leaves_the_defect_open(live):
    _open(live, extra="window.__script.redirect = {kind: 'refusal', playback: 'i cannot do that'};")
    live.evaluate("document.querySelector('.rv-redirect-text').value = 'delete the part'")
    _click(live, ".rv-redirect-go")
    assert "i cannot do that" in _text(live, ".rv-playback")
    assert live.evaluate("!document.querySelector('.rv-confirm')")
    assert "defect 1 message" in _text(live, ".rv-item")


def test_run_it_posts_and_the_unanswered_count_is_said_out_loud(live):
    _open(live)
    _click(live, ".rv-do")
    assert "7" in _text(live, ".rv-unanswered"), _text(live, ".rv-unanswered")
    _click(live, ".rv-run")
    live.wait_for("window.__done.length === 1", what="the run to be posted")
    post = live.evaluate("window.__calls.filter(c => c.url.endsWith('/review/run'))")
    assert len(post) == 1 and post[0]["method"] == "POST"
    done = live.evaluate("window.__done[0]")
    assert done["unanswered"] == 7 and done["ok"] is True
    assert "7" in done["said"] and "unanswered" in done["said"]
    assert live.evaluate("!document.querySelector('.rv')"), "the review stayed up after Run it"


def test_run_it_that_could_not_resume_says_try_again_and_stays(live):
    _open(live, extra="window.__script.run = {status: 503, body: {detail: 'nope'}};")
    _click(live, ".rv-run")
    assert "try again" in _text(live, ".rv-note").lower()
    assert live.evaluate("!!document.querySelector('.rv') && window.__done.length === 0")


def test_items_without_proposals_offer_leave_and_redirect_only(live):
    _open(live, review={**_review(), "groups": [_review()["groups"][2]]})
    assert live.evaluate("!document.querySelector('.rv-do')"), "Do that offered with no proposal"
    assert live.evaluate("!!document.querySelector('.rv-leave') && "
                         "!!document.querySelector('.rv-redirect-text')")
    # and there is no apply-all after it either: nothing was proposed to apply
    _click(live, ".rv-leave")
    assert live.evaluate("!document.querySelector('.rv-fixall')")


def test_the_parked_run_page_states_the_count_and_the_promise(live):
    out = live.evaluate("""(async () => {
      const { parkedCopy } = await import('/static/js/review/review.js');
      return parkedCopy({open: 8, decided: 0, groups: [1, 2, 3]});
    })()""")
    assert "8" in out and "nothing was changed without you" in out.lower(), out


def test_change_it_keeps_a_line_saying_the_earlier_answer_stands(live):
    _open(live)
    live.evaluate("document.querySelector('.rv-redirect-text').value = 'leave the fillet alone'")
    _click(live, ".rv-redirect-go")
    assert live.evaluate("document.activeElement.className") == "rv-confirm"
    _click(live, ".rv-change")
    assert "stays recorded until you replace it" in _text(live, ".rv-standing")


def test_a_refused_second_redirect_says_the_earlier_answer_still_stands(live):
    _open(live)
    live.evaluate("document.querySelector('.rv-redirect-text').value = 'leave the fillet alone'")
    _click(live, ".rv-redirect-go")
    _click(live, ".rv-change")
    live.evaluate("""window.__script.redirect = {kind: 'refusal', playback: 'i cannot do that'};
      document.querySelector('.rv-redirect-text').value = 'delete the part'""")
    _click(live, ".rv-redirect-go")
    text = _text(live, ".rv-playback").lower()
    assert "earlier answer still stands" in text and "nothing was recorded" not in text, text


def test_two_rapid_clicks_send_one_decision_and_skip_nothing(live):
    _open(live)
    live.evaluate("const b = document.querySelector('.rv-leave'); b.click(); b.click();")
    live.settle(0.3)
    assert len(live.evaluate("window.__calls")) == 1
    assert "defect 2 message" in _text(live, ".rv-item")


def test_a_decision_that_never_reaches_the_server_keeps_the_item_and_says_so(live):
    _open(live, extra="window.__script.reject = true;")
    _click(live, ".rv-leave")
    assert "didn't reach the server" in _text(live, ".rv-note")
    assert "defect 1 message" in _text(live, ".rv-item")
    assert live.evaluate("!document.querySelector('.rv-leave').disabled"), "controls stayed held"


def test_escape_in_the_redirect_box_does_not_close_the_review(live):
    _open(live)
    live.evaluate("""const t = document.querySelector('.rv-redirect-text'); t.focus();
      t.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));""")
    assert live.evaluate("!!document.querySelector('.rv')")
    live.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
    assert live.evaluate("!document.querySelector('.rv')")


def test_a_group_with_nothing_proposed_says_why_there_is_no_fix_all(live):
    base = _review()
    odd = base["groups"][2]
    extra = [_item(n, "odd", "ODD", proposal=False) for n in (9, 10)]
    odd["items"] += extra
    odd["count"] = 3
    _open(live, review={**base, "groups": [odd]})
    _click(live, ".rv-leave")
    # nothing to fix all with - and the sample was left, so leaving the rest is the one offer
    assert live.evaluate("!document.querySelector('.rv-fixall')")
    assert "Leave the rest" in _text(live, ".rv-leaverest")


def test_an_all_leave_sample_offers_leave_the_rest_never_fix_all(live):
    _open(live)
    _click(live, ".rv-leave")
    _click(live, ".rv-leave")
    assert live.evaluate("!document.querySelector('.rv-fixall')"), "fix-all offered after leaving the sample"
    assert "leave them" in _text(live, ".rv-why").lower()
    _click(live, ".rv-leaverest")
    calls = live.evaluate("window.__calls")
    assert calls[-1]["body"] == {"group_key": "gap", "action": "apply_all"}
    # the two in the sample and the two after it are answered: four of eight remain
    assert "4 defects still unanswered" in _text(live, ".rv-unanswered"), _text(live, ".rv-unanswered")


def test_a_redirect_in_the_sample_is_what_fix_all_repeats(live):
    _open(live)
    _click(live, ".rv-do")
    live.evaluate("document.querySelector('.rv-redirect-text').value = 'sew it from the other side'")
    _click(live, ".rv-redirect-go")
    _click(live, ".rv-confirm")
    assert "what you asked for in the sample" in _text(live, ".rv-why").lower()
    _click(live, ".rv-fixall")
    states = live.evaluate("window.__rv.groups[0].items.map(i => i.state)")
    assert states == ["accepted", "redirected", "redirected", "redirected"], states


def test_a_group_redirect_marks_what_it_covered_answered_and_does_not_ask_it_again(live):
    _open(live, extra="window.__script.redirect = {kind: 'directive', covered: ['item-2'], "
                      "playback: 'i will sew every one - 2 paired defects are left out: each one "
                      "needs its own review'};")
    live.evaluate("document.querySelector('.rv-redirect-text').value = 'sew all of them'")
    _click(live, ".rv-redirect-go")
    assert "left out" in _text(live, ".rv-playback")
    _click(live, ".rv-confirm")
    # item 2 was covered: the sample is over, and the rest are the two still open
    assert "defect 2 message" not in _text(live, ".rv-panel")
    assert "6 defects still unanswered" in _text(live, ".rv-unanswered"), _text(live, ".rv-unanswered")


def test_a_capped_review_says_it_shows_the_largest_of_how_many(live):
    _open(live, review={**_review(), "truncated": 492, "total": 500})
    assert "showing the 8 largest of 500" in _text(live, ".rv-truncated")
    live.evaluate("window.__rv.close()")
    _open(live)
    assert live.evaluate("!document.querySelector('.rv-truncated')")
