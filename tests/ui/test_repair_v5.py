# Responsibility: Verify the CRS v5 beta's customer surfaces in a real browser - the three-way choice in the geometry
#                 check, the repair lane's plain-language progress, the repair result (3D pins, the "how far faces
#                 moved" colouring, before/after, key numbers, trace, files) - and that none of them ever shows the
#                 repair's internal vocabulary, from the code's own strings or from hostile data.
# Boundaries: the console's own modules in the page; the network is stood in for with the contract's shapes
#             (docs/superpowers/specs/2026-11-26-crs-v5-beta-ux.md). It reads the DOM, never pixels.
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

REPO = Path(__file__).parents[2]
JOB = "55555555-5555-4555-b555-555555555555"
SESSION = "repair-session"


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (1.0, 0.5, 0.5))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _problem(n: int, kind: str, status: str, *, at=None, change=0.0, reason="") -> dict:
    return {"id": f"p{n}", "type": kind, "status": status, "change_mm": change, "reason_text": reason,
            "location": {"faces": [n], "bodies": [0], "points": [at or [100.0 * n, 250.0, 500.0]]}}


def _repair(**over) -> dict:
    r = {"engine": "v5", "choice": "meshing", "outcome": "partly_repaired",
         "problems": [_problem(1, "close_faces", "fixed", change=0.04),
                      _problem(2, "close_faces", "fixed", change=0.02),
                      _problem(3, "open_shell_volume", "not_fixed",
                               reason="The hole is too large to close without changing the part's shape"),
                      _problem(4, "closed_periodic_face", "left_as_is", reason="It does not stop meshing")],
         "meshers_passing": 3, "meshers_total": 4, "max_change_mm": 0.04, "allowed_change_mm": 0.1,
         "volume_ratio": 1.0004, "size_ratio": 1.0, "flags": [], "seconds": 250, "spend_usd": 0.4,
         "needs_review": False, "auto_accepted": False}
    r.update(over)
    return r


FILES = [
    {"id": "a1", "type": "repair_report", "label": "Repair report", "size": 2048, "url": "/d/report.json"},
    {"id": "a2", "type": "mesh_hints", "label": "Mesh hints", "size": 512, "url": "/d/hints"},
    {"id": "a3", "type": "repaired_cad", "label": "Repaired geometry", "size": 29208, "url": "/d/repaired"},
    {"id": "a4", "type": "returned_cad", "label": "Returned", "size": 20000, "url": "/d/returned"},
    {"id": "a5", "type": "repair_report_html", "label": "Repair report", "size": 4096, "url": "/d/report.html"},
    {"id": "a6", "type": "mesh_bundle", "label": "Everything", "size": 9999, "url": "/d/mesh"},
]

#: the repair surfaces as the API serves them: the part before, and after with how far each point moved (mm)
NETWORK = """
const SKIN = __SKIN__;
const b64 = (f) => { const u = new Uint8Array(f.buffer); let s = ''; for (const c of u) s += String.fromCharCode(c); return btoa(s); };
const after = JSON.parse(JSON.stringify(SKIN));
after.patches.forEach((p) => { const n = atob(p.positions_b64).length / 12; const d = new Float32Array(n);
  for (let i = 0; i < n; i++) d[i] = (i % 7) * 0.02; p.deviation_b64 = b64(d); });
const real = window.fetch.bind(window);
const json = (b, status = 200) => new Response(JSON.stringify(b), {status, headers: {'Content-Type': 'application/json'}});
window.fetch = (u, o) => {
  const s = String(u);
  if (s.includes('/repair/surfaces')) return Promise.resolve(window.__noSurfaces ? json({}, 404) : json({original: SKIN, repaired: after}));
  if (s.includes('/check/skin')) return Promise.resolve(json(SKIN));
  // the job read again as the panel opens: by default it says nothing newer
  if (/\\/api\\/v1\\/simulation\\/[^/?]+$/.test(s)) return Promise.resolve(json(window.__job || {status: 'succeeded'}));
  return real(u, o);
};
"""


def _network() -> str:
    return NETWORK.replace("__SKIN__", json.dumps(_skin()))


# ---------------------------------------------------------------- the choice ----

def test_the_geometry_check_offers_three_choices_with_repair_and_mesh_first(live):
    out = live.evaluate("""(async () => {
      const { formHtml, readForm } = await import('/static/js/render/geometry_form.js');
      const host = document.createElement('div'); document.body.appendChild(host);
      host.innerHTML = formHtml({flow:'internal', input_kind:'fluid-domain', openings:[]});
      const opts = [...host.querySelectorAll('.rc-opt')].map(l => ({
        label: l.querySelector('b').textContent, help: l.querySelector('.rc-opt-h').textContent,
        checked: l.querySelector('input').checked, named: !!l.getAttribute('for')}));
      const first = readForm(host, {});
      host.querySelector('.rc-opt input[value="off"]').click();
      const auto = host.querySelector('.rc-auto-box');
      const meshOnly = {body: readForm(host, {}), autoDisabled: auto.disabled};
      host.querySelector('.rc-opt input[value="standalone"]').click();
      auto.click();
      const repairOnly = readForm(host, {});
      const legend = host.querySelector('.rc-choice legend').textContent;
      host.remove();
      return {opts, first, meshOnly, repairOnly, legend};
    })()""")
    assert [o["label"] for o in out["opts"]] == ["Repair + mesh", "Repair CAD only", "Mesh only"]
    assert [o["checked"] for o in out["opts"]] == [True, False, False], "Repair + mesh is not the default"
    assert all(o["help"] and o["named"] for o in out["opts"]), "every option has one line of help and a label"
    assert out["legend"] == "What should we do with your CAD?"
    assert out["first"]["repair_goal"] == "meshing" and out["first"]["repair_auto_accept"] is False
    # Mesh only repairs nothing, so there is nothing to accept
    assert out["meshOnly"]["body"]["repair_goal"] == "off" and out["meshOnly"]["autoDisabled"] is True
    assert out["repairOnly"] == {**out["repairOnly"], "repair_goal": "standalone", "repair_auto_accept": True}


def test_a_choice_the_chat_made_is_shown_on_the_form(live):
    out = live.evaluate("""(async () => {
      const { formHtml } = await import('/static/js/render/geometry_form.js');
      const { feaPanelHtml, readFeaForm } = await import('/static/js/render/fea_form.js');
      const host = document.createElement('div'); document.body.appendChild(host);
      host.innerHTML = formHtml({flow:'internal', openings:[], repair_goal:'standalone', repair_goal_source:'chat'});
      const cfd = {checked: host.querySelector('.rc-opt input:checked').value,
                   said: (host.querySelector('.rc-said') || {}).textContent || ''};
      host.innerHTML = feaPanelHtml({}, {repair_goal:'off'});
      const fea = {checked: host.querySelector('.rc-opt input:checked').value, body: readFeaForm(host, {})};
      host.remove();
      return {cfd, fea};
    })()""")
    assert out["cfd"]["checked"] == "standalone"
    assert "Set from your message" in out["cfd"]["said"]
    # the FEA form carries the same choice and sends it
    assert out["fea"]["checked"] == "off" and out["fea"]["body"]["repair_goal"] == "off"


# ------------------------------------------------------------------ the lane ----

LANE = """
  const { Stage } = await import('/static/js/render/stage.js');
  const { dispatch } = await import('/static/js/core/events.js');
  Stage.mount();
  let cur = null;
  const ev = (e) => { cur = dispatch(e, Stage, cur); };
  const lane = () => { const n = document.querySelector('#tl .tl-node.rr-repair'); if (!n) return null;
    const live = n.querySelector('.rr-live');
    return {name: n.querySelector('.nm').textContent, count: n.querySelector('.ct').textContent,
            said: [...n.querySelectorAll('.rr-said .rr-line')].map(p => p.textContent),
            live: live.hidden ? '' : live.textContent,
            bar: (live.querySelector('.rr-bar > i') || {style:{}}).style.width || '',
            mark: [...n.querySelector('.tl-dot').classList].filter(c => c !== 'tl-dot').join(' '),
            first: n === document.querySelector('#tl').firstElementChild}; };
  Stage.ensureProc();
"""


def test_the_repair_lane_reads_in_plain_words_with_a_count(live):
    out = live.evaluate("(async () => {" + LANE + """
      const seen = {};
      ev({type:'repair_progress', stage:'repair', step:'checking'});
      seen.checking = lane();
      ev({type:'repair_progress', stage:'repair', step:'checking', problem_total:12});
      seen.found = lane();
      ev({type:'repair_progress', stage:'repair', step:'repairing', problem_index:3, problem_total:12, problem_type:'close_faces'});
      seen.third = lane();
      ev({type:'repair_progress', stage:'repair', step:'broader_repair', problem_index:9, problem_total:12});
      seen.broader = lane();
      ev({type:'repair_progress', stage:'repair', step:'verifying', problem_index:12, problem_total:12});
      seen.verify = lane();
      ev({type:'repair_progress', stage:'repair', step:'done', problem_total:12, problems_fixed:10});
      seen.done = lane();
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600});
      seen.rows = [...document.querySelectorAll('#tl .tl-node')].map(n => n.querySelector('.nm').textContent);
      seen.cursor = cur;
      return seen;
    })()""", timeout=60)
    assert out["checking"]["name"] == "Repairing your CAD" and out["checking"]["first"]
    assert out["checking"]["live"] == "Checking your CAD…"
    assert out["found"]["said"] == ["Found 12 problems to fix"]
    assert out["third"]["live"] == "Repairing a thin gap — problem 3 of 12…"
    assert out["third"]["count"] == "3 of 12" and out["third"]["bar"] == "25%"
    assert out["broader"]["live"] == "Trying a broader repair…"
    assert out["verify"]["live"] == "Checking the repaired part…"
    assert out["done"]["said"] == ["Found 12 problems to fix", "Repair finished: 10 of 12 fixed"]
    assert out["done"]["count"] == "10 of 12 fixed" and out["done"]["live"] == ""
    # two problems left over: the lane finished with a warning, never a plain tick
    assert "warn" in out["done"]["mark"], out["done"]
    # the lane leads the box, and the mesh's four rows follow it unchanged
    assert out["rows"] == ["Repairing your CAD", "Choosing the engine", "Designing the mesh",
                           "Creating the mesh", "Reviewing the mesh"]
    # a repair step's `stage` field never became the cursor a stage-less event joins
    assert out["cursor"] == "builder"
    assert_clean(live, "the repair lane")


def test_a_mesh_only_run_has_no_repair_lane_and_a_repair_only_run_has_only_it(live):
    out = live.evaluate("(async () => {" + LANE + """
      ev({type:'stage', stage:'builder'});
      const meshOnly = [...document.querySelectorAll('#tl .tl-node')].filter(n => !n.hidden).length;
      const noLane = lane() === null;
      Stage.newRun(); Stage.ensureProc();
      Stage.repairOnly();
      ev({type:'repair_progress', stage:'repair', step:'checking', problem_total:2});
      ev({type:'repair_progress', stage:'repair', step:'done', problem_total:2, problems_fixed:2});
      Stage.final({pass:true, attempts:0, repair:{choice:'standalone', problems:[]}});
      const shown = [...document.querySelectorAll('.proc')].pop();
      return {meshOnly, noLane,
              rows: [...shown.querySelectorAll('.tl-node')].filter(n => !n.hidden).map(n => n.querySelector('.nm').textContent),
              title: shown.querySelector('.proc-title').textContent, mark: lane().mark};
    })()""", timeout=60)
    assert out["noLane"] and out["meshOnly"] == 4
    assert out["rows"] == ["Repairing your CAD"], out
    assert out["title"] == "Repair finished" and "done" in out["mark"]


# ---------------------------------------------------------------- the result ----

def _open_result(live, data: dict, extra: str = "") -> dict:
    script = ("(async () => {" + _network() + extra + """
      const { openRepairResult } = await import('/static/js/render/repair.js');
      const anchor = document.createElement('div'); document.body.appendChild(anchor);
      const data = __DATA__;
      openRepairResult('__JOB__', anchor, data);
      const box = document.getElementById('repair-__JOB__');
      box.scrollIntoView();
      return {ok: !!box};
    })()""")
    return live.evaluate(script.replace("__DATA__", json.dumps(data)).replace("__JOB__", JOB), timeout=60)


def test_the_repair_result_shows_numbers_trace_files_and_the_part(live):
    _open_result(live, {"pass": True, "repair": _repair(), "files": FILES, "sessionId": SESSION})
    live.wait_for(f"!!(window._vdbg||{{}})['repair:{JOB}']", what="the repair view")
    out = live.evaluate("""(() => {
      const box = document.getElementById('repair-__JOB__');
      const v = window._vdbg['repair:__JOB__'];
      const before = v.state();
      v.colour('dev'); const dev = v.state();
      v.set('before'); const orig = v.state();
      v.set('after'); v.colour('pins');
      const focused = v.focus('p3');
      return {
        head: box.querySelector('.rp-verdict').textContent,
        keys: [...box.querySelectorAll('.rp-keys div')].map(d => [d.querySelector('dt').textContent, d.querySelector('dd').textContent]),
        files: [...box.querySelectorAll('.v-dl a')].map(a => [a.querySelector('b').textContent, a.getAttribute('href')]),
        types: [...box.querySelectorAll('.rr-types li')].map(l => l.textContent),
        trace: [...box.querySelectorAll('.rr-trace li')].map(l => [l.className, l.querySelector('.rr-t').textContent]),
        pins: [...box.querySelectorAll('.rp-pin')].map(p => [p.className, p.getAttribute('aria-label')]),
        legend: [...box.querySelectorAll('.rpv-legend span')].map(s => s.textContent),
        devLegend: !box.querySelector('.rpv-dev').hidden,
        before, dev, orig, focused, focal: v.state().focal,
      };
    })()""".replace("__JOB__", JOB))
    assert out["head"].startswith("CAD partly repaired.") and "2 of 4 problems fixed" in out["head"]
    keys = dict(out["keys"])
    assert keys["Problems fixed"] == "2 of 4" and keys["Meshers passing"].startswith("3 of 4")
    assert keys["Largest change"].startswith("0.04 mm") and "allowed 0.1 mm" in keys["Largest change"]
    assert keys["Volume change"] == "+0.04%" and keys["Size change"] == "no change"
    assert keys["Time"] == "4 min 10 s"
    # the repaired STEP leads; the customer's own file, the reports and the mesh hints follow; no mesh file here
    assert [f[0] for f in out["files"]] == ["Repaired STEP", "Your STEP, unchanged", "Repair report (HTML)",
                                            "Repair report (JSON)", "Mesh hints"], out["files"]
    assert out["types"][0].startswith("2 thin gaps") and "all fixed" in out["types"][0]
    assert out["trace"][0] == ["fixed", "Thin gap → Repaired it (faces moved up to 0.04 mm) → Fixed"]
    assert out["trace"][2][0] == "not_fixed" and "too large to close" in out["trace"][2][1]
    assert out["trace"][3] == ["left_as_is", "Seam problem → left as is: It does not stop meshing"]
    assert [[c for c in p[0].split() if c in ("fixed", "not_fixed", "left_as_is")][0] for p in out["pins"]] == ["fixed", "fixed", "not_fixed", "left_as_is"]
    assert out["pins"][0][1] == "Problem 1: thin gap, fixed"
    assert out["legend"] == ["Fixed", "Not fixed", "Left as is"]
    assert out["before"]["pins"] == 4 and out["before"]["before"] > 0 and not out["before"]["coloured"]
    # the colouring: faces coloured, the pins put away, the scale shown
    assert out["dev"]["coloured"] and out["dev"]["visiblePins"] == 0
    assert out["orig"]["which"] == "before"
    assert out["focused"] is True
    for got, want in zip(out["focal"], [0.3, 0.25, 0.5], strict=True):
        assert abs(got - want) < 1e-3, f"the camera is not on problem 3: {out['focal']}"
    assert_clean(live, "the repair result")


def test_repair_only_takes_the_stage_and_a_failed_mesh_still_shows_the_repair(live):
    out = live.evaluate("(async () => {" + _network() + """
      const { openRepairPanel } = await import('/static/js/render/repair.js');
      const anchor = document.createElement('div'); document.body.appendChild(anchor);
      // the mesh failed after the repair: the failure path draws the full repair result, not the thin panel
      openRepairPanel('j-fail', anchor, {pass:false, repair: __REPAIR__, files: __FILES__});
      const box = document.getElementById('repair-j-fail');
      return {full: box.classList.contains('rr-result'), head: box.querySelector('.rp-verdict').textContent};
    })()""".replace("__REPAIR__", json.dumps(_repair(outcome="repaired", problems=[
        _problem(1, "close_faces", "fixed")]))).replace("__FILES__", json.dumps(FILES)))
    assert out["full"] and out["head"].startswith("CAD repaired.")


def test_the_view_says_so_when_there_is_nothing_to_draw(live):
    _open_result(live, {"pass": True, "repair": _repair(), "files": []}, extra="window.__noSurfaces = true;")
    live.wait_for(f"/isn't available/.test(document.getElementById('repair-{JOB}').textContent)",
                  what="the no-view message")
    text = live.evaluate(f"document.getElementById('repair-{JOB}').textContent")
    # no file was delivered, but the report is still offered from the run's own routes
    assert "Repair report (HTML)" in text and "Repair report (JSON)" in text


# ----------------------------------------------------------- the language rule ----

def _terms(live) -> tuple[list[str], list[str]]:
    out = live.evaluate("""(async () => {
      const W = await import('/static/js/core/repair_words.js');
      return {terms: W.FORBIDDEN_TERMS, ids: Object.keys(W.PROBLEM_WORDS)};
    })()""")
    return out["terms"], [i for i in out["ids"] if "_" in i]


_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\"'`\\])//[^\n]*", re.S)
_STRING = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`", re.S)
_JSX_TEXT = re.compile(r">([^<>{}]*[A-Za-z][^<>{}]*)<")


def _strings(path: Path) -> list[str]:
    src = _COMMENT.sub("", path.read_text())
    found = [m.group(0)[1:-1] for m in _STRING.finditer(src)]
    if path.suffix == ".tsx":
        found += _JSX_TEXT.findall(src)
    return found


def _hits(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if re.search(rf"(^|[^a-z_]){re.escape(t)}([^a-z_]|$)", low)]


#: the customer's repair surfaces: every string they hold is held to the whole rule
REPAIR_SURFACES = ["ui/js/render/repair.js", "ui/js/render/repair_choice.js", "ui/js/viewer/repair_view.js",
                   "apps/console/src/app/_components/repair-settings.ts",
                   "apps/console/src/app/_components/repair-settings-view.tsx"]
#: identifiers a code string may hold as a key or selector, never as words shown (class names, data keys)
_CODE_STRING = re.compile(r"^[\w.\-#:\[\]=\"' >,*()$/@]*$")


def test_no_ui_string_names_the_repairs_internals(live):
    terms, ids = _terms(live)
    # THE NARROW TERMS ANYWHERE: no customer page may hold a search-strategy word or a class identifier
    narrow = [t for t in terms if t in ("tournament", "entrant", "rule seed", "seed rule", "conformal", "search_ai")] + ids
    files = [p for p in (REPO / "ui" / "js").rglob("*.js") if p.name != "repair_words.js"]
    files += [p for p in (REPO / "apps" / "console" / "src").rglob("*.ts*") if ".test." not in p.name]
    offenders = [f"{p.relative_to(REPO)}: {s!r}" for p in files for s in _strings(p) if _hits(s, narrow)]
    assert not offenders, "internal repair vocabulary in a UI string:\n  " + "\n  ".join(offenders)
    # THE WHOLE RULE ON THE REPAIR SURFACES: every word they show (strings that are only a selector or a key are code)
    wide = []
    for rel in REPAIR_SURFACES:
        for s in _strings(REPO / rel):
            words = re.sub(r"\$\{[^}]*\}", " ", s)
            if _CODE_STRING.match(words) and " " not in words.strip():
                continue
            if _hits(words, terms):
                wide.append(f"{rel}: {s!r}")
    assert not wide, "a repair surface's words break the language rule:\n  " + "\n  ".join(wide)


#: data an upstream leak could send: every free-text field carries an internal word or an exception
HOSTILE = _repair(
    problems=[_problem(1, "facet_crossing_local", "fixed"),
              _problem(2, "some_new_internal_class", "not_fixed",
                       reason="ValueError: tournament heat 3 entrant rule_seed failed"),
              _problem(3, "close_faces", "left_as_is",
                       reason='Traceback (most recent call last): File "x.py", line 3'),
              _problem(4, "low_quality", "not_fixed", reason="conformal pass rejected BRepAlgo_Fuse")],
    flags=["rule_seed_fallback", {"key": "x", "text": "fallback to search_ai"}, "spend_capped"],
    outcome="partly_repaired")


def test_hostile_data_never_puts_an_internal_word_on_the_page(live):
    terms, ids = _terms(live)
    _open_result(live, {"pass": False, "repair": HOSTILE, "files": FILES})
    lane = live.evaluate("(async () => {" + LANE + """
      ev({type:'repair_progress', stage:'repair', step:'checking', text:'Running tournament heat 1 of 3'});
      ev({type:'repair_progress', stage:'repair', step:'repairing', problem_index:1, problem_total:4,
          problem_type:'BRepCheck_NotClosed', text:'entrant rule_seed_close_faces'});
      ev({type:'repair_progress', stage:'repair', step:'broader_repair', text:'fallback: search_ai'});
      ev({type:'repair_progress', stage:'repair', step:'verifying', text:'conformal pass'});
      ev({type:'repair_progress', stage:'repair', step:'done', text:'KeyError: heat'});
      ev({type:'repair_progress', stage:'repair', step:'mystery', text:'TopoDS_Shape exception'});
      return document.querySelector('.proc').textContent;
    })()""", timeout=60)
    page = live.evaluate(f"document.getElementById('repair-{JOB}').innerText")
    for where, text in (("the repair result", page), ("the repair lane", lane)):
        bad = _hits(text, terms + ids) + [w for w in ("Traceback", "Error", "BRep", "TopoDS") if w in text]
        assert not bad, f"{where} shows internal words {bad}:\n{text}"
    # the hostile reasons were replaced by the page's own plain words, and the unknown type has a plain name
    assert "problem we couldn't identify" in page.lower()
    assert "Repairing a problem we couldn't identify — problem 1 of 4" in lane


# ------------------------------------------------- PR 2's wire and the owner's defaults ----

def test_a_progress_event_nested_under_progress_is_read_too(live):
    out = live.evaluate("""(async () => {
      const { progressLine } = await import('/static/js/core/repair_words.js');
      return progressLine({type:'repair_progress', stage:'repair',
        progress:{stage:'repairing', problem_index:2, problem_total:5, problem_type:'surface_crossing'}}).text;
    })()""")
    assert out == "Repairing intersecting surfaces — problem 2 of 5"


def test_unmeasured_points_are_not_coloured_as_no_move_and_the_v3_finish_is_not_said(live):
    out = live.evaluate("""(async () => {
      const { deviationColour } = await import('/static/js/viewer/repair_view.js');
      const { flagLines, problemName } = await import('/static/js/core/repair_words.js');
      return {unmeasured: deviationColour(-1, 0.1), zero: deviationColour(0, 0.1),
              v3: flagLines({engine:'v3', flags:['engine_switched']}),
              bypass: flagLines({review_bypass:true}),
              named: problemName('a_new_class', 'Sharp edge'), hostile: problemName('a_new_class', 'rule_seed x')};
    })()""")
    assert out["unmeasured"] != out["zero"], "a point that was never measured reads as 'did not move'"
    assert out["v3"] == [], "the customer was told which repair method finished the job"
    assert out["bypass"] and "accepted automatically" in out["bypass"][0]
    assert out["named"] == "sharp edge" and out["hostile"] == "problem we couldn't identify"


def test_beside_a_mesh_the_repair_is_a_tab_next_to_it(live):
    out = live.evaluate("(async () => {" + _network() + """
      const { openRepairResult } = await import('/static/js/render/repair.js');
      const host = document.createElement('div'); document.body.appendChild(host);
      const mesh = document.createElement('div'); mesh.className = 'viewer'; mesh.id = 'viewer-tabbed';
      host.appendChild(mesh);
      openRepairResult('tabbed', null, {pass:true, repair: __REPAIR__, files: []}, {tabWith: mesh});
      const repair = document.getElementById('repair-tabbed');
      const tabs = () => [...document.querySelectorAll('#rr-tabs-tabbed [role=tab]')].map(
        t => [t.textContent, t.getAttribute('aria-selected')]);
      const first = {tabs: tabs(), mesh: mesh.hidden, repair: repair.hidden};
      document.querySelector('#rr-tabs-tabbed [data-k=repair]').click();
      const second = {tabs: tabs(), mesh: mesh.hidden, repair: repair.hidden};
      const reports = [...repair.querySelectorAll('.rr-rep')].map(a => a.textContent);
      return {first, second, reports};
    })()""".replace("__REPAIR__", json.dumps(_repair())))
    assert out["first"] == {"tabs": [["Mesh", "true"], ["Repair", "false"]], "mesh": False, "repair": True}
    assert out["second"] == {"tabs": [["Mesh", "false"], ["Repair", "true"]], "mesh": True, "repair": False}
    # no report was delivered as a file: both are offered from the run's own report routes
    assert [r[:20] for r in out["reports"]] == ["Repair report (HTML)", "Repair report (JSON)"]


def test_the_lane_bar_is_neutral_not_the_error_red(live):
    out = live.evaluate("(async () => {" + LANE + """
      ev({type:'repair_progress', stage:'repair', step:'repairing', problem_index:1, problem_total:4,
          problem_type:'close_faces'});
      const bar = document.querySelector('.rr-repair .rr-bar > i');
      const probe = document.createElement('i');
      probe.style.color = getComputedStyle(document.documentElement).getPropertyValue('--accent');
      document.body.appendChild(probe);
      const accent = getComputedStyle(probe).color; probe.remove();
      return {bar: getComputedStyle(bar).backgroundColor, accent};
    })()""")
    assert out["bar"] != out["accent"], "the running repair bar is drawn in the accent red"


def test_a_run_may_skip_review_only_when_the_organisation_allows_it(live):
    out = live.evaluate("""(async () => {
      const { formHtml, readForm } = await import('/static/js/render/geometry_form.js');
      const host = document.createElement('div'); document.body.appendChild(host);
      host.innerHTML = formHtml({flow:'internal', openings:[], repair_auto_accept_allowed:false, repair_auto_accept:true});
      const box = host.querySelector('.rc-auto-box');
      const locked = {disabled: box.disabled, checked: box.checked, help: host.querySelector('.rc-auto .rc-opt-h').textContent};
      host.querySelector('.rc-opt input[value="standalone"]').click();
      locked.afterPick = box.disabled;
      locked.body = readForm(host, {});
      host.innerHTML = formHtml({flow:'internal', openings:[], repair_auto_accept_allowed:true});
      const open = {disabled: host.querySelector('.rc-auto-box').disabled};
      host.remove();
      return {locked, open};
    })()""")
    assert out["locked"]["disabled"] and not out["locked"]["checked"] and out["locked"]["afterPick"]
    assert "An admin can allow skipping it" in out["locked"]["help"]
    assert out["locked"]["body"]["repair_auto_accept"] is False
    assert out["open"]["disabled"] is False


def test_a_repair_only_run_waiting_for_a_worker_says_so_on_its_lane(live):
    out = live.evaluate("(async () => {" + LANE + """
      Stage.repairOnly();
      Stage.waiting('waiting for a worker to start (about 8 minutes)');
      return lane().live;
    })()""")
    assert "mesh" not in out.lower(), out


def test_the_panel_follows_a_repair_section_written_after_the_ending(live):
    # slug crsv5ui, 26 Nov: the ending carried `ran: false` and no files; the section and the report landed a moment
    # later. The panel reads the job once as it opens and shows the job as it now stands.
    late = _repair(outcome="unchanged", problems=[])
    _open_result(live, {"pass": False, "repair": {"ran": False, "choice": "standalone", "problems": []}, "files": []},
                 extra="window.__job = " + json.dumps({"status": "failed", "repair_goal": "standalone", "repair": late,
                                                      "artifacts": [{"id": "r1", "artifact_type": "repair_report",
                                                                     "label": "Repair report", "size_bytes": 900,
                                                                     "download_url": "/d/r.json"}]}) + ";")
    live.wait_for(f"/No problems found/.test(document.getElementById('repair-{JOB}').innerText)",
                  what="the newer repair section")
    text = live.evaluate(f"document.getElementById('repair-{JOB}').innerText")
    assert "Repair report (JSON)" in text and "No repair files were kept" not in text
