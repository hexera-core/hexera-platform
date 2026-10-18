// Responsibility: Give the meshing engineer the full-screen review of a parked repair run - the
//                 groups of unsure defects as a rail, one defect per screen with the part pinned
//                 and framed on it, three ways to answer, and "Run it" to resume the run.
// Owns: the review's screen state (which group, which item, which phase), its reduced 3D view,
//       and the words on every screen.
// Boundaries: it decides nothing about a defect and starts no run itself - every answer is a
//             POST the API judges, and "Run it" hands the ending back to the page that opened it.

/* THE REVIEW. The repair run parked itself because the rules were unsure about some defects. The
 * engineer sees them a group at a time: the first few of a group (its sample) one by one, then -
 * if the rest could be treated the same way - one question: fix all N the same way, or keep going.
 * Nothing is applied without an answer, and "Run it" is always there: whatever is unanswered
 * goes through unreviewed, and the screen says how many.
 *
 * WHAT THE API FORCES ON A REDIRECT. The route records a redirect the moment it is interpreted,
 * in the same call that returns the playback, so "confirm before it records" cannot be built
 * as asked. The screen says the interpretation back at once, says that it is recorded, and does
 * not move on until the engineer says "that's right"; "change it" reopens the box (a new redirect,
 * "Leave it" or "Do that" each overwrite the recorded one).
 */
import { getReview, postReviewDecision, postReviewRun } from "../api/endpoints.js";
import { getGeometrySkin } from "../api/endpoints.js";
import { esc } from "../core/format.js";
import { b64f32, b64u32, loadVtk } from "../viewer/geometry_stage.js";
import { bindMouse } from "../viewer/view_controls.js";

const BACKGROUND = [0.035, 0.055, 0.075], BODY = [0.74, 0.78, 0.84], SEL = "#ff4f00";

/* ---------- words ---------- */

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** What the parked run's page says: how many defects wait, and the promise. */
export function parkedCopy(review) {
  const open = Number((review || {}).open || 0);
  const groups = ((review || {}).groups || []).length;
  return `this run stopped for you before changing anything: ${plural(open, "defect is", "defects are")} `
    + `waiting in ${plural(groups, "group", "groups")}. nothing was changed without you.`;
}

const OP_WORDS = { sew: "sew the gap shut" };
function proposalLine(item) {
  const p = item.proposal;
  if (!p || !p.op) return "";
  return `${OP_WORDS[p.op] || p.op.replaceAll("_", " ")}${p.rationale ? " - " + p.rationale : ""}`;
}

/** The size a defect is ordered by: its largest measurement. */
export function sizeOf(item) {
  const vals = Object.values(item.measurements || {}).filter((v) => typeof v === "number");
  return vals.length ? Math.max(...vals.map(Math.abs)) : 0;
}

const unanswered = (groups) => groups.reduce(
  (n, g) => n + g.items.filter((i) => i.state === "open").length, 0);

/** The mint cap's honesty line: the review holds the largest of a larger number. "" when not capped. */
export function truncatedCopy(review) {
  const cut = Number((review || {}).truncated || 0);
  if (!(cut > 0)) return "";
  const total = Number(review.total || 0), shown = Math.max(total - cut, 0);
  return `showing the ${shown} largest of ${total} - the other ${cut} were too many to put to you one by one.`;
}

const STANDING = '<p class="rv-standing">your earlier answer stays recorded until you replace it.</p>';

function unansweredLine(n) {
  return n ? `${plural(n, "defect", "defects")} still unanswered - they go through unreviewed.`
    : "every defect is answered.";
}

/* ---------- the reduced 3D view: the part, a pin, the camera on the defect ---------- */

/* The geometry stage's machinery in the small: the same skin, the same HTML pin that follows its
   point every frame, the same look-at-the-entity camera move. It has no openings, no form and no
   measuring - the bar is pin + camera-to-entity + orbit. */
async function mountView(host, sessionId, jobId) {
  if (!sessionId) return null;
  let surf;
  try { surf = await getGeometrySkin(sessionId); await loadVtk(); } catch { return null; }
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: BACKGROUND });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  const scale = () => { const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR))); };
  grw.resize();
  host.addEventListener("contextmenu", (e) => e.preventDefault());
  bindMouse(grw);
  (surf.patches || []).forEach((pt) => {
    let pts, polys, normals = null;
    if (pt.polys_b64) { pts = b64f32(pt.points_b64); polys = b64u32(pt.polys_b64);
      if (pt.normals_b64) normals = b64f32(pt.normals_b64); }
    else { pts = b64f32(pt.positions_b64); const n = pts.length / 9; polys = new Uint32Array(n * 4);
      for (let t = 0; t < n; t++) polys.set([3, t * 3, t * 3 + 1, t * 3 + 2], t * 4); }
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
    if (normals && normals.length === pts.length) {
      pd.getPointData().setNormals(vtk.Common.Core.vtkDataArray.newInstance(
        { name: "Normals", values: normals, numberOfComponents: 3 }));
    }
    const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
    const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper);
    const pr = actor.getProperty(); pr.setColor(...BODY); pr.setInterpolationToPhong();
    pr.setAmbient(0.32); pr.setDiffuse(0.76); pr.setSpecular(0.12); pr.setSpecularPower(20);
    ren.addActor(actor);
  });
  const cam = ren.getActiveCamera();
  cam.azimuth(45); cam.elevation(25); ren.resetCamera(); rw.render();
  const bs = ren.computeVisiblePropBounds();
  const diag = Math.max(Math.hypot(bs[1] - bs[0], bs[3] - bs[2], bs[5] - bs[4]), 1e-9);
  const bounds = bs.slice();

  const pin = document.createElement("div");
  pin.className = "rv-pin"; pin.style.display = "none";
  host.appendChild(pin);
  let at = null;                       // the defect's point, in the skin's units (metres)
  const fit = () => { ren.resetCamera(bounds); rw.render(); };
  const ro = new ResizeObserver(() => { scale(); if (!at) fit(); else rw.render(); });
  ro.observe(host);

  function place() {
    if (!at) { pin.style.display = "none"; return false; }
    const size = apiRW.getSize(), aspect = size[0] / Math.max(size[1], 1), rect = host.getBoundingClientRect();
    const nd = ren.worldToNormalizedDisplay(at[0], at[1], at[2], aspect);
    const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
    pin.style.display = ok ? "" : "none";
    if (ok) { pin.style.left = (nd[0] * rect.width) + "px"; pin.style.top = ((1 - nd[1]) * rect.height) + "px"; }
    return ok;
  }
  let alive = true;
  (function loop() { if (!alive || !document.body.contains(host)) return; place(); requestAnimationFrame(loop); })();

  /** Put the pin on the defect and bring the camera to it, looking down its normal. */
  function show(camera) {
    const c = ((camera || {}).centroid_mm || []);
    if (c.length !== 3) { at = null; fit(); return; }
    at = c.map((v) => v / 1000);
    let n = (camera.normal || [0, 0, 0]).slice();
    const L = Math.hypot(n[0], n[1], n[2]);
    n = L > 1e-9 ? n.map((v) => v / L) : [-0.5, -0.5, 0.7];
    const reach = Math.max(((camera.extent_mm || 0) / 1000) * 4, 0.3 * diag);
    cam.setFocalPoint(...at);
    cam.setPosition(at[0] + n[0] * reach, at[1] + n[1] * reach, at[2] + n[2] * reach);
    cam.setViewUp(...(Math.abs(n[2]) < 0.9 ? [0, 0, 1] : [0, 1, 0]));
    ren.resetCameraClippingRange(); rw.render(); place();
  }
  scale(); fit();
  window._vdbg = window._vdbg || {};
  window._vdbg["review:" + jobId] = {
    focal: () => cam.getFocalPoint(), pin: () => at, visible: () => (place() ? 1 : 0),
    camera: () => cam.getPosition(),
  };
  return { show, stop() { alive = false; ro.disconnect(); delete window._vdbg["review:" + jobId]; } };
}

/* ---------- the review ---------- */

/** Open the full-screen review for a job. Resolves to a controller once the review is drawn.
 *  `opts.onRun({ok, unanswered, said})` hears "Run it" succeed; `opts.onClose()` the way back to
 *  the run page. An `offered` review (the run was delivered) is read, not decided. */
export async function openReview(jobId, opts = {}) {
  document.getElementById("rv-" + jobId)?.remove();
  const review = await getReview(jobId);
  const readonly = review.status !== "open";
  const groups = review.groups || [];
  const itemById = new Map(groups.flatMap((g) => g.items.map((i) => [i.id, i])));

  const root = document.createElement("div");
  root.className = "rv"; root.id = "rv-" + jobId;
  root.setAttribute("role", "dialog"); root.setAttribute("aria-label", "Review the unsure defects");
  root.innerHTML = `<header class="rv-head">
      <b class="rv-title">Review before the repair runs</b>
      <span class="rv-unanswered"></span>
      ${truncatedCopy(review) ? `<span class="rv-truncated">${esc(truncatedCopy(review))}</span>` : ""}
      <button type="button" class="rv-back">Back to the run</button>
      ${readonly ? "" : '<button type="button" class="rv-run">Run it</button>'}
    </header>
    <aside class="rv-rail"><ol></ol></aside>
    <main class="rv-main"><div class="rv-view"></div><section class="rv-panel"></section></main>
    <footer class="rv-note" role="status"></footer>`;
  document.body.appendChild(root);
  const $ = (s) => root.querySelector(s);
  const panel = $(".rv-panel"), note = $(".rv-note");

  let gi = -1, phase = "sample", queue = [], shownIdx = 0, shownTotal = 0, view = null, closed = false, busy = false;
  let pendingCamera = null;

  mountView($(".rv-view"), review.geometry_session_id, jobId).catch(() => null).then((v) => {
    if (closed) { v?.stop(); return; }       // the review was closed while the part loaded
    view = v;
    if (!v) $(".rv-view").innerHTML = '<p class="rv-noview">the part can\'t be shown here - '
      + "you can still decide; the defect is described beside it.</p>";
    else if (pendingCamera) v.show(pendingCamera);
  });

  /* ONE ANSWER IN FLIGHT. A second click while a POST is out would send a second decision and
     skip a defect; the controls are held until the server has answered, and a request that never
     arrives leaves the defect on screen with a note. */
  async function guarded(fn) {
    if (busy) return;
    busy = true;
    const held = [...root.querySelectorAll(".rv-panel button, .rv-panel textarea, .rv-run")];
    held.forEach((b) => { b.disabled = true; });
    try { await fn(); }
    catch { say("that didn't reach the server - try again."); }
    finally { busy = false; held.forEach((b) => { b.disabled = false; }); }
  }
  const frame = (camera) => { pendingCamera = camera; if (view) view.show(camera); };

  const say = (t) => { note.textContent = t || ""; };
  function paintRail() {
    $(".rv-rail ol").innerHTML = groups.map((g, i) => {
      const cls = i < gi || (phase === "done") ? "done" : i === gi ? "current" : "todo";
      return `<li class="${cls}" data-group="${esc(g.group_key)}"><b>${esc(g.defect_code)}</b>`
        + `<span>${plural(g.count, "defect", "defects")}</span></li>`;
    }).join("");
    $(".rv-unanswered").textContent = unansweredLine(unanswered(groups));
  }

  function finishedScreen() {
    phase = "done"; paintRail(); frame(null);
    panel.innerHTML = `<h2>that is every group</h2><p>${esc(unansweredLine(unanswered(groups)))}
      ${readonly ? "" : "run it whenever you are ready."}</p>`;
  }
  function enterGroup(i) {
    gi = i;
    if (gi >= groups.length) return finishedScreen();
    const g = groups[gi];
    queue = g.sampled_item_ids.map((id) => itemById.get(id))
      .filter((it) => it && (readonly || it.state === "open"));
    phase = "sample"; shownIdx = 0; shownTotal = queue.length;
    advance();
  }
  const sampleIds = (g) => new Set(g.sampled_item_ids);
  const restOf = (g) => g.items.filter((i) => !sampleIds(g).has(i.id) && i.state === "open");

  function advance() {
    const g = groups[gi];
    // a defect answered meanwhile (a group redirect covered it) is not asked again
    if (!readonly) while (queue.length && queue[0].state !== "open") queue.shift();
    if (queue.length) { shownIdx += 1; return itemScreen(queue[0]); }
    if (phase === "sample" && !readonly && restOf(g).length) return applyAllScreen();
    enterGroup(gi + 1);
  }

  function itemScreen(item) {
    paintRail(); frame(item.camera); say("");
    const meas = Object.entries(item.measurements || {}).map(
      ([k, v]) => `<li>${esc(k.replaceAll("_", " "))}: <b>${esc(v)}</b></li>`).join("");
    const prop = proposalLine(item);
    panel.innerHTML = `<div class="rv-step">${shownIdx} of ${shownTotal}</div>
      <article class="rv-item">
        <h2>${esc(item.defect_code)} <small>${esc(item.entity_type)} ${esc(item.entity_index)}</small></h2>
        <p class="rv-msg">${esc(item.message)}</p>
        ${meas ? `<ul class="rv-meas">${meas}</ul>` : ""}
        ${prop ? `<p class="rv-prop">the repair would ${esc(prop)}</p>`
          : "<p class=\"rv-prop\">nothing was proposed for this one.</p>"}
        ${item.pairing_key ? "<p class=\"rv-paired\">this defect is paired with another; its answer changes what the other means.</p>" : ""}
      </article>
      ${readonly ? '<div class="rv-controls"><button type="button" class="rv-next">Next</button></div>'
        : `<div class="rv-controls">
          ${prop ? '<button type="button" class="rv-do">Do that</button>' : ""}
          <button type="button" class="rv-leave">Leave it</button>
        </div>
        <div class="rv-redirect"><label>or say what to try instead
          <textarea class="rv-redirect-text" rows="2" placeholder="e.g. leave the fillet alone"></textarea></label>
          <button type="button" class="rv-redirect-go">Try this instead</button>
          <div class="rv-playback-slot"></div></div>`}`;
    if (readonly) { panel.querySelector(".rv-next").onclick = () => { queue.shift(); advance(); }; return; }
    panel.querySelector(".rv-do")?.addEventListener("click", () => guarded(() => decide(item, "accept", "accepted")));
    panel.querySelector(".rv-leave").onclick = () => guarded(() => decide(item, "leave", "left"));
    panel.querySelector(".rv-redirect-go").onclick = () => guarded(() => redirect(item));
    if (item.state === "redirected") panel.querySelector(".rv-playback-slot").innerHTML = STANDING;
  }

  /* A GROUP REDIRECT ANSWERS THE MEMBERS IT COVERED (never a paired one - the server leaves those
     out and says so), and a new answer on the same defect hands them back: the server reopens
     them, and so does the screen. */
  function cover(item, ids) {
    const now = new Set(ids || []);
    (item.covered || []).filter((id) => !now.has(id)).forEach((id) => {
      const m = itemById.get(id); if (m && m.state === "redirected") { m.state = "open"; m.coveredBy = null; } });
    now.forEach((id) => { const m = itemById.get(id); if (m) { m.state = "redirected"; m.coveredBy = item.id; } });
    item.covered = [...now];
  }

  async function decide(item, action, state) {
    const r = await postReviewDecision(jobId, { item_id: item.id, action });
    if (!r.ok) return say(failure(r, "that answer was not saved"));
    item.state = state; item.decided_at = new Date().toISOString(); cover(item, []);
    queue.shift(); advance();
  }

  async function redirect(item) {
    const box = panel.querySelector(".rv-redirect-text");
    const text = (box.value || "").trim();
    if (!text) return say("say what you want tried instead, then send it.");
    const r = await postReviewDecision(jobId, { item_id: item.id, action: "redirect", text });
    if (!r.ok) return say(failure(r, "that was not understood by the server"));
    const slot = panel.querySelector(".rv-playback-slot");
    if (r.data.kind === "directive") {
      item.state = "redirected"; item.decided_at = new Date().toISOString();
      cover(item, r.data.covered); paintRail();
      slot.innerHTML = `<div class="rv-playback"><p>${esc(r.data.playback)}</p>
        <p class="rv-recorded">that is recorded as your answer for this defect.</p>
        <button type="button" class="rv-confirm">That's right</button>
        <button type="button" class="rv-change">Change it</button></div>`;
      slot.querySelector(".rv-confirm").onclick = () => { queue.shift(); advance(); };
      slot.querySelector(".rv-change").onclick = () => { slot.innerHTML = STANDING; box.value = ""; box.focus(); };
      slot.querySelector(".rv-confirm").focus();
    } else {
      slot.innerHTML = `<div class="rv-playback rv-refused"><p>${esc(r.data.playback || r.data.reason || "that cannot be done")}</p>
        <p>${item.state === "redirected" ? "your earlier answer still stands."
          : "nothing was recorded; leave it, or say it another way."}</p></div>`;
    }
  }

  /* THE SAMPLE'S SETTLED DIRECTION, as the server reads it: every sampled answer "leave it" means
     leave the rest; otherwise the latest answer that was not a leave (an accept, or a redirect
     whose operation the rest receive). A defect a group redirect covered is not a sample answer. */
  const STATE_DIRECTION = { accepted: "accept", redirected: "redirect", left: "leave" };
  function sampleDirection(g) {
    const answered = g.sampled_item_ids.map((id) => itemById.get(id))
      .filter((i) => i && STATE_DIRECTION[i.state] && !i.coveredBy && !(i.decision || {}).covered_by)
      .sort((a, b) => (Date.parse(a.decided_at) || 0) - (Date.parse(b.decided_at) || 0));
    if (!answered.length) return null;
    const acted = answered.filter((i) => i.state !== "left");
    return acted.length ? STATE_DIRECTION[acted[acted.length - 1].state] : "leave";
  }

  function applyAllScreen() {
    const g = groups[gi], rest = restOf(g);
    paintRail(); frame(null); say("");
    const direction = g.apply_all_allowed ? sampleDirection(g) : null;
    const fixable = direction === "redirect" || (direction === "accept" && !!g.proposed_op);
    const leaveable = direction === "leave";
    const why = !g.apply_all_allowed
      ? "<p class=\"rv-why\">these defects come in pairs, and the answer for one changes what its partner means, so each one needs its own answer - there is no fix-all for this group.</p>"
      : leaveable ? "<p class=\"rv-why\">you left every one in the sample as it is, so the one answer on offer for the rest is the same: leave them.</p>"
      : direction === "redirect" ? "<p class=\"rv-why\">the rest get what you asked for in the sample, each one tried on its own.</p>"
      : !g.proposed_op ? "<p class=\"rv-why\">nothing was proposed for this group, so there is nothing to apply to all of it.</p>" : "";
    panel.innerHTML = `<h2>${plural(rest.length, "more", "more")} like these</h2>
      <p>that was the sample. ${fixable || leaveable ? "if the rest are the same, one answer covers them." : ""}</p>${why}
      <div class="rv-controls">
        ${fixable ? `<button type="button" class="rv-fixall">Fix all ${rest.length} the same way</button>` : ""}
        ${leaveable ? `<button type="button" class="rv-leaverest">Leave the rest</button>` : ""}
        <button type="button" class="rv-onebyone">Keep reviewing one by one</button>
        <button type="button" class="rv-largest">Show me the largest first</button>
      </div>`;
    const go = (items) => { queue = items; phase = "rest"; shownIdx = 0; shownTotal = items.length; advance(); };
    panel.querySelector(".rv-onebyone").onclick = () => go(rest);
    panel.querySelector(".rv-largest").onclick = () => go(rest.slice().sort((a, b) => sizeOf(b) - sizeOf(a)));
    const applyAll = () => guarded(async () => {
      const r = await postReviewDecision(jobId, { group_key: g.group_key, action: "apply_all" });
      if (!r.ok) return say(failure(r, "that was not applied"));
      const state = { leave: "left", redirect: "redirected", accept: "accepted" }[(r.data || {}).direction || direction];
      const at = new Date().toISOString();
      rest.forEach((i) => { i.state = state; i.decided_at = at; });
      queue = []; phase = "rest"; advance();
    });
    panel.querySelector(".rv-fixall")?.addEventListener("click", applyAll);
    panel.querySelector(".rv-leaverest")?.addEventListener("click", applyAll);
  }

  function failure(r, lead) {
    const d = (r.data || {}).detail;
    return `${lead}${d ? ": " + (typeof d === "string" ? d : "see the server's reply") : ""}. try again.`;
  }

  /* ---------- the way out ---------- */
  function close() { closed = true; view?.stop(); root.remove(); document.removeEventListener("keydown", onKey); }
  const onKey = (e) => {
    const t = e.target, typing = t && /^(TEXTAREA|INPUT)$/.test(t.tagName || "");
    if (e.key === "Escape" && !typing && !busy) { close(); opts.onClose?.(); } };
  document.addEventListener("keydown", onKey);
  $(".rv-back").onclick = () => { close(); opts.onClose?.(); };

  const runBtn = $(".rv-run");
  if (runBtn) runBtn.onclick = () => guarded(async () => {
    const left = unanswered(groups);
    const r = await postReviewRun(jobId).catch(() => ({ ok: false, status: 0, data: {} }));
    if (r.ok) {
      const said = `running it. ${left ? plural(left, "defect", "defects") + " unanswered went through unreviewed."
        : "every defect was answered."} nothing was changed without you.`;
      close();
      return opts.onRun?.({ ok: true, unanswered: left, said });
    }
    if (r.status === 503) return say("the run could not be resumed just now. your answers are kept - try again in a moment.");
    if (r.status === 409) return say(((r.data || {}).detail || "this run is not waiting for a review")
      + " - if it was delivered, run it again from the chat.");
    say("the run could not be started. try again.");
  });

  paintRail();
  if (!groups.length) finishedScreen(); else enterGroup(0);
  return { close, root, groups };
}

/** A quiet way into the review beside a finished run's panel: asks whether this run has an
 *  offered review and, if so, draws one line that opens it. Draws nothing otherwise. */
export async function offerReview(jobId, anchorEl, opts = {}) {
  if (!anchorEl || document.getElementById("rv-offer-" + jobId)) return null;
  let review;
  try { review = await getReview(jobId); } catch { return null; }
  if (!review || review.status !== "offered" || !review.open) return null;
  const el = document.createElement("div");
  el.className = "rv-offer"; el.id = "rv-offer-" + jobId;
  el.innerHTML = `<span>${esc(plural(review.open, "defect", "defects"))} the repair was unsure about.</span>
    <button type="button" class="rv-offer-open">Look at them</button>`;
  el.querySelector("button").onclick = () => openReview(jobId, opts);
  anchorEl.appendChild(el);
  return el;
}

/** The parked run's page: the count and the promise, with the way into the review. */
export async function openParked(jobId, anchorEl, opts = {}) {
  if (!anchorEl) return null;
  document.getElementById("rv-parked-" + jobId)?.remove();
  let review;
  try { review = await getReview(jobId); } catch { return null; }
  if (!review || review.status !== "open") return null;
  const el = document.createElement("div");
  el.className = "rv-parked"; el.id = "rv-parked-" + jobId;
  el.innerHTML = `<p>${esc(parkedCopy(review))}</p><button type="button" class="rv-parked-open">Review them</button>`;
  el.querySelector("button").onclick = () => openReview(jobId, opts);
  anchorEl.appendChild(el);
  return el;
}
