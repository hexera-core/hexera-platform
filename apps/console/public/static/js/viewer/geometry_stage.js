// Responsibility: Show the geometry check as a stage the user can orient in - the part in 3D drawn
// the way a CAD viewer draws it, a numbered sticker on every opening, and the panel beside it
// where names and roles are confirmed, stickers added or removed. A whole thermal model (an ECXML
// upload) gets its own mode instead: the parts in the colours of the panel's list, picked from
// either side, the case seen through, the file's air box as faint lines - and no stickers.
// Owns: its vtk.js scene (the skin, its edges, the lights, the sticker balls and the pins that
// follow them), the naming state of the panel, the measuring ruler, and the takeover of the
// workbench while the check is open.
// Boundaries: it decides nothing about the geometry - positions, sizes and proposals arrive from
// the check, the holes an added opening snaps to among them - and it starts no run; confirming
// is the callback it was handed.

/* THE GEOMETRY STAGE.
 *
 * The stage opens the moment the part is measured, with the measuring step's own labels under a
 * banner that says what is happening, and fills in the model's labels when they arrive - the
 * user is already turning the part while the model thinks, and can fix the labels and proceed
 * without waiting for it. Proceeding hands the workbench back, and the conversation carries on
 * where it was.
 */
import { getGeometrySkin } from "../api/endpoints.js";
import { esc } from "../core/format.js";
import { applyFlow, bindAxis, bindUnit, changedAnswers, editsLine, extentAlong, followSuggestion, followUnit, formHtml,
  markConfirmed, noteDrawn, readExternal, readForm, rowHtml, servedProposal, shown, tableHead, unitChoiceHint,
  unitChoiceNeeded, unitOf } from "../render/geometry_form.js";
import { bindFeaPanel, bindMeasure, feaPanelHtml, followAssembly, followClean, followMeasure, interfaceMarks, lightSpotRow } from "../render/fea_form.js";
import { bindThermalPanel, partDetails, thermalPanelHtml, watts } from "../render/thermal_form.js";
import { circleThrough, clickIndex, firstHit, snapAt } from "./open_ends.js";
import { bindMouse, isUpAxis, orient, saveUp, savedUp, shield, upSelectHtml, upVector } from "./view_controls.js";

let _vtkP = null;
export function loadVtk() {
  if (window.vtk) return Promise.resolve();
  if (!_vtkP) _vtkP = new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = "/static/vendor/vtk.js";
    s.onload = res; s.onerror = () => rej(new Error("vtk.js failed to load"));
    document.head.appendChild(s); });
  return _vtkP;
}
export function b64f32(b) { const bin = atob(b), n = bin.length, u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = bin.charCodeAt(i); return new Float32Array(u.buffer); }
export function b64u32(b) { const bin = atob(b), n = bin.length, u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = bin.charCodeAt(i); return new Uint32Array(u.buffer); }

/* the console's own palette: the mesh viewer's dark background, a light grey part with soft
   highlights, darker hairlines on the sharp corners; stickers in the yellow of the pictures, the
   selected one in the product's orange, which nothing else on the stage wears */
const BACKGROUND = [0.035, 0.055, 0.075];
const BODY = [0.74, 0.78, 0.84], EDGE = [0.30, 0.34, 0.40];
const STICKER = [1.0, 0.83, 0.0], SEL = [1.0, 0.31, 0.0];
// an FEA part's hot spots: a quiet steel blue, apart from the stickers' yellow
const SPOT = [0.42, 0.62, 0.86];
// an assembly's interfaces: a muted green where parts touch, a muted red where they pass into each other
const TOUCH = [0.40, 0.72, 0.52], OVERLAP = [0.86, 0.45, 0.40];

function spherePd(cx, cy, cz, r) {
  const la = 12, lo = 16, pts = [], polys = [];
  for (let i = 0; i <= la; i++) { const th = Math.PI * i / la;
    for (let j = 0; j <= lo; j++) { const ph = 2 * Math.PI * j / lo;
      pts.push(cx + r * Math.sin(th) * Math.cos(ph), cy + r * Math.cos(th), cz + r * Math.sin(th) * Math.sin(ph)); } }
  const W = lo + 1;
  for (let i = 0; i < la; i++) for (let j = 0; j < lo; j++) { const a = i * W + j; polys.push(4, a, a + 1, a + W + 1, a + W); }
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  pd.getPoints().setData(Float32Array.from(pts), 3);
  pd.getPolys().setData(Uint32Array.from(polys));
  return pd;
}

/* THE WORKBENCH TAKEOVER, the same move the mesh viewer makes: with a workbench on the page the
   stage fills it and the conversation becomes the drawer; without one it opens inline. Releasing
   gives the page back exactly as it was unless another viewer is still waiting in the workbench. */
function takeOver(box, anchorEl) {
  const wb = document.getElementById("workbench"), app = document.getElementById("app");
  if (!(wb && app)) { if (anchorEl) anchorEl.after(box); return () => box.remove(); }
  wb.querySelectorAll(".viewer").forEach((v) => { v.hidden = true; });
  wb.appendChild(box);
  app.classList.add("wb"); app.classList.remove("wb-collapsed");
  const tg = document.getElementById("wb-toggle");
  if (tg) { tg.hidden = false;
    tg.onclick = () => { const on = app.classList.toggle("wb-collapsed");
      tg.textContent = on ? "Show conversation" : "Hide conversation";
      tg.setAttribute("aria-pressed", on ? "true" : "false"); }; }
  return () => {
    box.remove();
    const left = [...wb.querySelectorAll(".viewer")];
    if (left.length) { left[left.length - 1].hidden = false; return; }
    app.classList.remove("wb", "wb-collapsed");
    if (tg) { tg.hidden = true; tg.textContent = "Hide conversation"; tg.setAttribute("aria-pressed", "false"); }
  };
}

function leadHtml(p) {
  const size = (p.size_mm || []).map((v) => shown(v, p)).join(" x ");
  return `<div class="gc-part"><b>${esc(p.part || "the part")}</b>${size ? `<span class="gc-size">${esc(size)} <span class="gc-u">${unitOf(p)}</span></span>` : ""}</div>
    <p>Turn the part and click a sticker to select its row. Fix any name or role, add or remove a
    sticker, then proceed: the questions that follow skip everything confirmed here.</p>`;
}

/** THE STAGE'S BOX: the panel on one side, the part's canvas on the other with the up-axis
 *  picker, Fit, and the axes that turn with the part. Written once because every way in draws the
 *  same box and only the panel's contents differ. */
function stageBox(sessionId, p, panelHtml) {
  const id = "gstage-" + sessionId;
  document.getElementById(id)?.remove();
  const box = document.createElement("div"); box.className = "viewer gc-stage"; box.id = id;
  box.innerHTML = `<div class="v-bar gc-panel">${panelHtml}</div>
    <div class="v-canvas gc-canvas" id="gs-canvas-${sessionId}">
      <div class="v-loading" id="gs-load-${sessionId}"><div>Loading the part…</div></div>
      <div class="v-tools">${upSelectHtml("gs-up-" + sessionId, savedUp("session", sessionId) || (isUpAxis(p.up_axis) ? p.up_axis : "+z"))}<button class="v-btn v-tool" id="gs-fit-${sessionId}" type="button" title="Frame the whole part">Fit</button></div>
      <svg class="gc-axes" viewBox="0 0 84 84" aria-hidden="true">
        ${["x", "y", "z"].map((k) => `<line class="ax ax-${k}" x1="42" y1="42" x2="42" y2="42"/><text class="ax-l ax-${k}" x="42" y="42">${k.toUpperCase()}</text>`).join("")}
      </svg>
    </div>`;
  return box;
}

/* WHAT STANDS WHERE THE FORM WILL BE, while the chat has not yet said what the mesh is for. */
const WAITING_NOTE = `<div class="gc-note">Say in the chat what this mesh is for and the rest of
  the check opens here, with the one form to confirm it on.</div>`;

/** Open the check as a stage. `d` is the check the API served (its `proposal` is what is shown;
 *  `named: false` opens the stage with the measuring step's labels under a banner that says what
 *  is happening, and `update` brings the model's when they arrive), `confirm(body)` records the
 *  answer, `opts.retry(step)` runs a step of the check again and resolves to the check as it then
 *  stands. `opts.fallback()` is called instead when the stage cannot be drawn - no skin, no
 *  WebGL - so the user still gets the card, EXCEPT while `route` is "": which form the card would
 *  carry is exactly what is not settled then, and the caller tries again once it is. */
export async function openGeometryStage(sessionId, d, confirm, opts) {
  opts = opts || {};
  // A WHOLE THERMAL MODEL (an ECXML upload) is shown as the file's own model, not as a part with
  // openings to name: its own panel and scene, below
  if (d && d.thermal) return openThermalStage(sessionId, d, confirm, opts);
  // AN FEA PART (the user's words said structural) is shown as itself: its size, unit, bodies and
  // clean check - no flow form, no openings, no stickers
  if (d && d.fea) return openFeaStage(sessionId, d, confirm, opts);
  const p = d.proposal || {};
  // what the check proposed, kept apart from the form the user edits: Proceed says back what changed
  let served = servedProposal(null, p);
  /* THE PART OPENS ON ITS OWN AND THE FORM IS WHAT WAITS. `route` is the empty string the server
     sends until the user's first answer settles what the mesh is for (application/
     fea_check.stage_route), and WHICH form a part is answered on depends on it - an FEA part must
     never be shown the flow form and its stickers. The console used to wait for the route before
     it opened anything at all, so a customer uploaded their CAD and then saw chat text and a
     progress line for one whole turn, at the moment of highest doubt about whether their file had
     been read. So the stage opens as soon as there is a skin, with a note where its form will be,
     and `growForm` builds the form in place the moment the route lands: ONE stage and ONE scene for
     the session. A payload with no `route` key at all is an already-routed flow check, as every
     caller of this function has always passed.

     AT `ready` THE ROUTE IS AS SETTLED AS IT WILL EVER GET, so there is nothing left to wait for
     and the form is built at once. The route can stay "" for good: purpose_map.question_for
     returns "" once the one purpose question has been ASKED, not only once it is settled
     (purpose_map.py:716), and intake/message.py:543 queues the naming on that - so a customer who
     answers "not sure" has their part named and the check reach `ready` while stage_route still
     answers "" (fea_check.py:39). The composer's watch ENDS on that poll (`if (!waitingFull)
     return;`), so whatever is on screen then is final, and a waiting note that says "say in the
     chat what this mesh is for" is a dead end: nothing is left to grow the form.
     MEASURED in Chrome against clean HEAD, the composer driven from a real upload, status ready
     with route "": HEAD showed the form - 2 opening rows and a working Proceed - on a stage that
     drew, and the same form as a CARD where it could not. Without this, the tree showed the
     waiting note with 0 rows and 0 Proceed on the drawing, and NOTHING AT ALL where it could not
     draw (0 cards: the fallback below is gated on this same formKind).
     An FEA part never reaches this line - openFeaStage is taken on `d.fea` above - so the gate's
     real purpose, never a flow form on an FEA part, is kept for the whole scouted phase, which is
     the only phase in which the route can still land. `flow` is also the server's own default for
     anything that is not FEA (fea_check.stage_route). */
  let formKind = d.route === "" && d.status !== "ready" ? "" : "flow";
  const box = stageBox(sessionId, p, `
      <div class="v-row v-titlerow"><b class="v-title">Geometry check</b><span class="v-meta">what Hexera sees</span></div>
      <div class="gc-banner" hidden><span class="gc-spin" aria-hidden="true"></span><span class="gc-banner-text"></span><span class="gc-banner-act"></span></div>
      <div class="gc-lead">${formKind ? leadHtml(p) : waitingLeadHtml(p)}</div>
      <div class="gc-form">${formKind ? formHtml(p) : WAITING_NOTE}</div>`);
  if (!formKind) box.classList.add("gc-waiting");
  const release = takeOver(box, opts.anchorEl);
  const panel = box.querySelector(".gc-panel");
  const form = panel.querySelector(".gc-form");
  if (formKind) { applyFlow(form); bindUnit(panel, p); bindAxis(form, p); noteDrawn(form); }
  const banner = panel.querySelector(".gc-banner");
  // the FEA panel's own follower, set by `growForm` if the route lands as "fea" (feaPanel)
  let feaFollow = null;
  /* THE FORM IS NEVER LOCKED. The user can fix the measuring step's labels and proceed at any
     point; the banner says what is happening around them - whether the file said its unit (a
     triangle file carries none; the unit box beside the sizes is where to say, or the chat),
     whether the model was asked to name the openings, whether it answered, or gave up - and
     what to do about it. */
  let naming = false, stalled = false, unitNeeded = !!d.unit_needed, shown = "";
  // the stage opened on the quick preview of a big file: the full reading is still to land
  let previewShown = d.preview === "running";
  const STALL_MS = opts.stallMs || 3 * 60 * 1000;
  let askedAt = null, stallTimer = null;
  function showBanner(text, cls, retryStep) {
    banner.hidden = !text; banner.className = "gc-banner" + (cls ? " " + cls : "");
    banner.querySelector(".gc-banner-text").textContent = text || "";
    const act = banner.querySelector(".gc-banner-act"); act.innerHTML = "";
    if (text && retryStep && opts.retry) {
      // THE WAY ON when the naming gave up: run it again from here, over the same facts
      const b = document.createElement("button"); b.type = "button"; b.className = "gc-retry"; b.textContent = "Try again";
      b.onclick = () => {
        b.disabled = true; b.textContent = "Starting…";
        Promise.resolve(opts.retry(retryStep))
          .then((d3) => { askedAt = null; stalled = false; shown = ""; update(d3); })
          .catch((e) => { showBanner("Could not run it again: " + ((e && e.message) || "try later"), "warn", retryStep); });
      };
      act.appendChild(b);
    }
  }
  function stateBanner() {
    if (unitNeeded) return ["The file does not say its unit: check the size beside the unit box below and pick the unit that makes it right, or answer in the chat. The names are the measuring step's own - fix them and proceed.", ""];
    if (naming) return ["Naming the openings… turn the part meanwhile, or fix the names and proceed now.", "busy"];
    if (stalled) return ["The naming is taking longer than usual. These names are the measuring step's own: fix them and proceed, or wait.", "warn"];
    return ["These names are the measuring step's own. Answer the question in the chat and I'll name the openings - or fix them here and proceed.", ""];
  }
  function showState() {
    const key = ["scouted", naming, stalled, unitNeeded].join("|");
    if (key === shown) return;                 // the poll repeating a state changes nothing
    shown = key;
    const [text, cls] = stateBanner();
    showBanner(text, cls);
    form.classList.toggle("gc-naming", naming);
  }
  // A NAMING THAT NEVER COMES: once the model was asked, if nothing has arrived after a while
  // the banner says so and stops waiting. The model's names still fill any row the user has
  // not touched when they do arrive.
  function noteAsked(d1) {
    if (!(d1 && d1.naming_requested) || askedAt !== null) return;
    askedAt = Date.now();
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => { if (!naming) return; naming = false; stalled = true; showState(); }, STALL_MS);
  }
  // the banner's words are about the openings' names, so it waits for the form that holds them
  if (formKind && d.named === false) { naming = !!d.naming_requested; noteAsked(d); showState(); }

  let scene = null;
  try {
    await loadVtk();
    const surf = await getGeometrySkin(sessionId);
    scene = initScene(sessionId, box, surf, p);
    document.getElementById("gs-load-" + sessionId)?.remove();
  } catch (e) {
    // the stage is a better way to answer the same form, never the only way
    console.warn("geometry stage unavailable, showing the card instead:", e && e.message);
    release();
    // NO CARD WHILE THE ROUTE CAN STILL LAND. The card is a form too, and WHICH form is exactly
    // what is not known yet - a flow card on an FEA part is the wrongness the route gate exists
    // to prevent - so the composer tries the stage once more when the route lands, and once more
    // at `ready`, which is what gives the card its pictures. It is the SAME formKind the form
    // above is built from, so a check that has reached `ready` with no route gets its card here:
    // it is the last poll of the watch, and suppressing the card there gave the customer nothing
    // at all (measured 0 cards against HEAD's 1, with its 2 rows and a pressable Proceed).
    if (formKind && opts.fallback) opts.fallback();
    return null;
  }

  function bindProceed() {
    const btn = form.querySelector(".gc-proceed"), hint = form.querySelector(".gc-hint");
    if (!btn) return;          // the waiting note stands where the form will be: nothing to press
    btn.onclick = async () => {
      // an opening placed off any measured face has no size until the user types one; the
      // confirm never receives a mouth of no size
      const blank = [...form.querySelectorAll(".gc-dia")].find((i) => !(Number(i.value) > 0));
      if (blank) {
        const tr = blank.closest("tr");
        hint.textContent = `Opening ${tr ? tr.dataset.id : ""} needs a size first: type how many mm across.`;
        blank.focus();
        return;
      }
      // THE UNIT IS PICKED, NOT PASSED: when the part's size makes the unit in effect doubtful
      // ("117 mm" for a wind turbine blade) Proceed never takes either reading in silence
      if (unitChoiceNeeded(p)) {
        hint.textContent = unitChoiceHint(p);
        const pick = form.querySelector(".gc-pick"); if (pick) pick.focus();
        return;
      }
      const body = readForm(form, p);
      btn.disabled = true; btn.textContent = "Confirming…";
      try {
        await confirm(body, editsLine(served, body, p));     // the chat says back what the user changed
        markConfirmed(form);
        scene.stop();
        release();                       // the workbench goes back; the conversation carries on
      } catch (e) {
        btn.disabled = false; btn.textContent = "Looks right, proceed";
        hint.textContent = "Could not confirm: " + ((e && e.message) || "try again");
      }
    };
  }
  bindProceed();

  // the drawing the server stored is re-fetched when the full reading has replaced it (skinFollower)
  const followSkin = skinFollower(sessionId, d, () => scene);

  /* THE FORM IS BUILT WHERE THE WAITING NOTE STANDS, the moment the route lands. The stage and its
     scene are not touched: the part the user is already turning stays on the page, and so does
     anything they have typed. A stage REBUILT on every route change lost a typed opening name and
     role outright, because `route` is a pure function of ALL the user's messages (purpose_map.
     reading_for joins every user text) - so "and I want the stress in the bracket too", typed while
     a slow naming was still running, re-read every message, flipped the route and took the form
     away mid-answer. update() is documented not to move the form for precisely that reason.

     WHICH form is what the route decides, and that is the gate's real purpose kept: an FEA part
     gets the FEA panel, never the flow form and its stickers. A thermal model is not a case here -
     its own file decides it (fea_check.stage_route reads check_mode off the stored record, which
     the scout writes before any skin exists), so a thermal check's route is "thermal" on the first
     poll that could open a stage at all, and it opened on the thermal stage. */
  function growForm(d2) {
    const want = d2.fea ? "fea" : String(d2.route || "");
    if (want !== "fea" && want !== "flow") return;        // still unsettled; thermal never lands here
    formKind = want;
    box.classList.remove("gc-waiting");
    /* THE FORM IS BUILT FROM THE PAYLOAD THAT CARRIED THE ROUTE, not from the one before it. `p`
       was last assigned by update() on the previous UNROUTED poll, and the FEA panel's own
       follower moves the unit, the bodies and the clean check but NOT the size - so a panel built
       from `p` showed a stale size under its own question, "Is this your part and are the units
       right?", and readFeaForm sends p.size_mm and p.scale_to_m on Proceed (render/fea_form.js
       :268). MEASURED in Chrome: a stage opened on a preview proposal of 1000x500x500, then one
       update carrying route fea AND a proposal of 2000x900x900, read "1000 x 500 x 500 mm" and
       CONFIRMED size_mm [1000, 500, 500] - beside a bodies line of 3, which the follower does
       move. The flow branch below has always done this; it is the same line, where both branches
       reach it. */
    Object.assign(p, d2.proposal || {});
    if (formKind === "fea") {
      clearTimeout(stallTimer);              // an FEA part has no openings, so no naming to wait on
      showBanner("");
      feaFollow = feaPanel(box, panel, d2.fea || {}, p,
        { confirm, done: () => { scene.stop(); release(); }, scene: () => scene });
      scene.rebind();       // the panel's own Measure button; it holds no rows, so still no stickers
      /* THE MARKS GO ON THE PART, AND THE PANEL'S ROWS ARE BOUND TO THEM. feaPanel sets
         `p.hot_spots` from the measurement it was handed and initScene draws them from it - but on
         THIS way in the scene was built before the panel existed, and rebind() touches neither the
         marks nor the rows. The panel's own follower will not do it either when the payload that
         carried the route ALSO carried the measurement: followMeasure compares the `data-state`
         feaPanelHtml has just written from that same payload (render/fea_form.js:125, :207), so it
         reports no change. And that is the ORDINARY ordering, not a rare one - the naming asks no
         model for an FEA part (application/geometry_check.py:1768), so it is fast and its
         measurement lands on the poll the route does. MEASURED in Chrome: route and measurement
         together gave spots [] and 0 marks against 2 hot-spot rows in the panel, and pointing at a
         row lit nothing; openFeaStage, which builds the panel BEFORE the scene, gave [h1, h2].
         This is that same setSpots - the one the follower calls and the one initScene calls -
         reached from the one way in that could not get to it. */
      scene.setSpots(p.hot_spots || []);
      return;
    }
    // read fresh: this form was built from THIS payload, not from the one the stage opened on, so
    // whether the quick drawing is still standing is what the payload in hand says
    previewShown = d2.preview === "running";
    panel.querySelector(".gc-lead").innerHTML = leadHtml(p);
    form.innerHTML = formHtml(p);
    applyFlow(form); bindUnit(panel, p); bindAxis(form, p); noteDrawn(form); bindProceed(); scene.rebind();
    shown = "";
  }

  /** The check moved on. Scouted: only the banner and Proceed follow it. Ready: the form is
   *  re-drawn from the model's proposal and the banner goes. Failed: the naming gave up, or
   *  never answered - the measuring step's labels stand, and the naming can be run again from
   *  here. Positions and sizes are the code's and do not move; the stickers stay where they are.
   *  The drawing itself is the one thing that can change under it (followSkin) - and the form
   *  itself arrives once (growForm), when the route settles which form it is. */
  function update(d2) {
    if (!d2) return;
    followSkin(d2);
    if (!formKind) growForm(d2);
    if (!formKind) {
      // nothing to answer yet: the part's own lead line is all there is to follow
      if (d2.proposal) {
        Object.assign(p, d2.proposal);
        panel.querySelector(".gc-lead").innerHTML = waitingLeadHtml(p);
      }
      return;
    }
    if (formKind === "fea") { feaFollow(d2); return; }
    unitNeeded = !!d2.unit_needed;
    const q = d2.proposal || {};
    if (d2.proposal) served = servedProposal(served, d2.proposal);
    followUnit(panel, p, q.unit, q.unit_basis);      // a unit settled in the chat reaches an untouched box
    followSuggestion(panel, p, q.unit_suggestion);   // the other reading, as the server now sees it
    if ((d2.status === "scouted" || d2.status === "ready") && previewShown && d2.preview !== "running"
        && !d2.named_on_preview) {
      // THE FULL READING LANDED on a stage opened on the quick preview of a big file: its openings,
      // flat faces and holes come in - unless the user already set openings of their own, which
      // stand; what they changed on the form stays either way
      previewShown = false;
      const p2 = d2.proposal || {};
      if (!(p.openings || []).length) {
        // the poll may meet the full reading already named (ready): it comes in the same way, and
        // the ready re-draw below then takes the model's words onto these openings
        const chosen = p.unit_touched ? { unit: p.unit, unit_basis: p.unit_basis, unit_touched: true } : {};
        // the flow axis the user turned and the reference length they typed outlive it, as on the
        // ready re-draw
        const answered = {};
        if (p.flow_axis_touched) Object.assign(answered, { flow_axis: p.flow_axis, flow_axis_guessed: false, flow_axis_touched: true });
        if (p.reference_length_typed) Object.assign(answered, { reference_length_mm: p.reference_length_mm, reference_length_typed: true });
        p.user_set = { ...(p.user_set || {}), ...changedAnswers(form) };
        // THE SKIN WAS DRAWN AT THE PREVIEW'S SCALE: a naming that re-read the part in another unit
        // serves lengths at the new one, so they are read back at the drawing's (the server re-reads
        // the form from `scale_to_m` on confirm, as for every length on this stage)
        const mine = p.scale_to_m || 0.001, theirs = p2.scale_to_m || mine;
        const back = Math.abs(theirs - mine) > 1e-12 ? mine / theirs : 1;
        const incoming = back === 1 ? p2 : Object.fromEntries(Object.entries(p2)
          .filter(([k]) => !/^(scale_to_m|unit|unit_basis|unit_assumed)$/.test(k))
          .map(([k, v]) => [k, atScale(k, v, back)]));
        Object.assign(p, incoming, chosen, answered, p.user_set);
        panel.querySelector(".gc-lead").innerHTML = leadHtml(p);
        form.innerHTML = formHtml(p);
        applyFlow(form); bindUnit(panel, p); bindAxis(form, p); noteDrawn(form); bindProceed(); scene.rebind();
        shown = "";
      } else {
        if (p2.faces) p.faces = p2.faces;
        if (p2.holes) p.holes = p2.holes;
      }
    }
    if (d2.status === "scouted") {
      if (d2.naming_requested && !stalled) naming = true;
      noteAsked(d2);
      showState();
      return;
    }
    clearTimeout(stallTimer);
    naming = false; stalled = false;
    form.classList.remove("gc-naming");
    const p2 = d2.proposal || {};
    const failed = d2.status === "failed";
    if (!failed) {
      // keep the code's measurements, take the model's words - IN PLACE: the scene holds the
      // same object, and what the user adds or removes there is what Proceed reads. A row the
      // user already changed (after a stall opened the form) keeps the user's words.
      const byId = new Map((p2.openings || []).map((o) => [o.id, o]));
      const typed = new Map([...form.querySelectorAll(".gc-table tbody tr")].map((tr) => [Number(tr.dataset.id),
        { name: (tr.querySelector(".gc-name") || {}).value, role: (tr.querySelector(".gc-role") || {}).value }]));
      const merged = (p.openings || []).map((o) => {
        const m = byId.get(o.id), u = typed.get(Number(o.id));
        if (u && u.name !== undefined && (u.name !== (o.name || "") || u.role !== o.role)) return { ...o, name: u.name, role: u.role };
        return m ? { ...o, name: m.name, role: m.role, confidence: m.confidence } : o; });
      // THE STAGE KEEPS THE SCOUT'S OWN NUMBERS. A naming that re-read the facts in the confirmed
      // unit serves them re-read, but the scene and its skin were drawn from the scout's reading,
      // so every length here stays in it: the unit box says which unit that is, and the server
      // re-reads the form from `scale_to_m` when it is confirmed. Only the words move.
      const mine = p.scale_to_m || 0.001, theirs = p2.scale_to_m || mine;    // a missing scale is the display's default
      const rescaled = Math.abs(theirs - mine) > 1e-12;
      const words = rescaled ? Object.fromEntries(Object.entries(p2).filter(([k]) => !/_mm$|_m$|^faces$|^holes$/.test(k))) : p2;
      // a unit the user set in the box outlives the re-draw; a served one fills an untouched box
      const chosen = p.unit_touched ? { unit: p.unit, unit_basis: p.unit_basis, unit_touched: true } : {};
      // so do the flow axis the user turned and the reference length they typed; an untyped
      // length is the part's length along the axis that stands
      const answered = {};
      if (p.flow_axis_touched) Object.assign(answered, { flow_axis: p.flow_axis, flow_axis_guessed: false });
      if (p.reference_length_typed) answered.reference_length_mm = p.reference_length_mm;
      // ...and so does every other answer the user changed on the form - the kind, the flow, the
      // far-field box, the ground - which the re-draw used to put back to the check's values
      p.user_set = { ...(p.user_set || {}), ...changedAnswers(form) };
      Object.assign(p, words, { openings: merged }, chosen, answered, p.user_set);
      if (p.flow_axis_touched && !p.reference_length_typed) {
        const along = extentAlong(p, p.flow_axis); if (along !== null) p.reference_length_mm = along;
      }
      panel.querySelector(".gc-lead").innerHTML = leadHtml(p);
      form.innerHTML = formHtml(p);
      applyFlow(form); bindUnit(panel, p); bindAxis(form, p); noteDrawn(form); bindProceed(); scene.rebind();
      shown = "ready"; showBanner("");
    } else {
      const key = "failed|" + (d2.reason || "");
      if (key !== shown) {
        shown = key;
        showBanner("The naming step gave up" + (d2.reason ? " (" + d2.reason + ")" : "")
          + ". The names below are the measuring step's own: fix them and proceed"
          + (d2.retry && opts.retry ? ", or try the naming again." : "."), "warn", d2.retry);
      }
    }
    scene.refresh();
  }

  /* `hasForm` is how the chat tells a stage with something to answer on from the part shown before
     the route landed: a check that fails under the latter is, to the chat, a check with no stage,
     so the box is released rather than told to go and press a Proceed that is not there. */
  return { release: () => { clearTimeout(stallTimer); scene.stop(); release(); }, update,
           hasForm: () => !!formKind, isNaming: () => naming };
}

/** THE DRAWING CHANGES UNDER THE STAGE, ONCE. A big STEP - the normal semiconductor case - always
 *  opens the stage on the PREVIEW, the welded lump stored within seconds of the upload so that
 *  something is on screen; the full reading then replaces the stored skin with the file's own named
 *  solids. The stage fetched the skin exactly once and `update` never re-fetched, so the fix on the
 *  server alone would have changed nothing a user sees.
 *
 *  THE SIGNAL IS THE POLL'S OWN, no new endpoint and nothing to subscribe to, and it is READ OFF
 *  EVERY RECORD rather than latched when the stage is built. `preview` says the stored skin is
 *  still the quick drawing - "running" while the scout is measuring it, "kept" when the full
 *  reading gave up and the preview IS the answer (geometry_check.kept_preview) - and
 *  `named_on_preview` says the naming landed on it first and the full reading is still to come. A
 *  record with none of those is one whose stored skin is the full reading's own.
 *
 *  THE BUILD-TIME LATCH WAS THE WHOLE DEFECT, both ways round. It read `d.preview === "running"`
 *  once, at build, and geometry_check._land_named POPS `preview` from a record it names on the
 *  preview - so a stage first built in that state (status ready, named_on_preview true, no preview
 *  key, which is exactly what the measured first customer assembly was in: session fb5e5589, the
 *  naming landing at 1,022.2 s against a scout that needed 1,303.9 s) was disarmed for good and
 *  never took the named solids: measured 1 patch, ["skin"], 1 fetch. And since the only disarm test
 *  was `=== "running"`, `preview: "kept"` FIRED it instead - a whole skin re-download and scene
 *  rebuild to draw exactly what was already on screen, on the 15.9 MB-class assemblies this path
 *  exists for, throwing the user's clicks away.
 *
 *  `preview_reason` IS THE KEY THAT SURVIVES A KEPT PREVIEW, and reading the absence of the other
 *  two as the full reading only moved the same defect one poll later. `preview: "kept"` is popped
 *  exactly as `running` is: _land_named pops `preview` off the result it writes and write_status
 *  stores that result on its own, merging no old record back (geometry_check.py:148, :1827) - so
 *  the READY record written over a kept preview has no `preview` key and no `named_on_preview`,
 *  while the stored skin is still the preview's welded lump, because the full reading never
 *  finished and never will: kept_preview is what the scout giving up stores. `preview_reason` does
 *  survive that write, and only a kept-preview lineage ever carries it - merged_onto_full takes its
 *  keys from the FULL reading (geometry_check.py:1859), which has none. MEASURED on the real
 *  kept_preview -> store_status -> _land_named chain against a fake store: the ready record's keys
 *  are facts, named, naming_seconds, pictures, preview_reason, proposal, purpose_text, session_id,
 *  skin, skin_key, status, vision, written_at, with preview None and named_on_preview None. In
 *  Chrome, running then kept then that record, the fetch count went 1 -> 1 -> 2 and the scene was
 *  rebuilt - click index discarded, actors, edges and legend replaced - to draw the identical lump.
 *
 *  Returns a function to hand each poll; it fetches at most once and never throws into the caller -
 *  a fetch that fails leaves the preview's drawing, which is the part, correctly drawn, in one piece. */
function skinFollower(sessionId, d, scene) {
  const isFull = (r) => !!r && (r.status === "scouted" || r.status === "ready")
    && !r.preview && !r.named_on_preview && !r.preview_reason;
  // what is on screen: the full reading's own solids already, or the quick drawing
  let drawn = isFull(d);
  return (d2) => {
    if (drawn || !isFull(d2)) return;
    drawn = true;                                       // at most one re-fetch in a session
    getGeometrySkin(sessionId)
      .then((surf) => { const s = scene(); if (s && s.setSkin) s.setSkin(surf); })
      .catch((e) => console.warn("the fuller drawing of the part could not be fetched:", e && e.message));
  };
}

/** The names of a thermal model's placed parts beside their colours, in the stage's corner. */
function partsLegend(patches) {
  const el = document.createElement("div"); el.className = "gc-legend";
  el.style.cssText = "position:absolute;left:8px;bottom:8px;max-height:45%;overflow:auto;padding:6px 8px;"
    + "border-radius:6px;background:rgba(255,255,255,0.88);color:#2a2f36;font-size:11px;line-height:1.5;z-index:2";
  const named = patches.filter((pt) => pt.name), rows = named.slice(0, 40);
  el.innerHTML = rows.map((pt) => {
    const c = (Array.isArray(pt.color) ? pt.color : BODY).map((v) => Math.round(v * 255)).join(",");
    return `<div><span style="display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;background:rgb(${c})"></span>${esc(pt.name)}</div>`;
  }).join("") + (named.length > rows.length ? `<div>and ${named.length - rows.length} more</div>` : "");
  return el;
}

function initScene(sessionId, box, surf, p) {
  const host = document.getElementById("gs-canvas-" + sessionId);
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: BACKGROUND });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  function setRenderScale() { const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR))); }
  grw.resize();
  host.addEventListener("contextmenu", (e) => e.preventDefault());
  bindMouse(grw);                     // left-drag turns, right-drag pans, the wheel zooms

  /* THE LIGHTS: a key from the upper left, a fill from the right and a faint rim from behind,
     all riding with the camera, so a turned part is always lit the way a CAD viewer lights it */
  if (vtk.Rendering.Core.vtkLight) {
    [[-0.7, 0.9, 1.0, 0.62], [0.9, 0.25, 0.7, 0.28], [0.0, -0.4, -1.0, 0.14]].forEach(([x, y, z, i]) => {
      const l = vtk.Rendering.Core.vtkLight.newInstance();
      if (l.setLightTypeToCameraLight) l.setLightTypeToCameraLight();
      l.setPosition(x, y, z); l.setFocalPoint(0, 0, 0); l.setIntensity(i); l.setColor(1, 1, 1);
      ren.addLight(l);
    });
  }

  /* THE SKIN - shared vertices, smooth normals broken at sharp corners, from the worker; the
     older soup form still draws, flat. Built through a function because the drawing is replaced
     once in a session: see `setSkin` below. */
  const skins = [];
  let edgeCount = 0, edgeActor = null, legendEl = null, drawnNames = [];
  function buildSkin(s) {
    (s.patches || []).forEach((pt) => {
      let pts, polys, normals = null;
      if (pt.polys_b64) { pts = b64f32(pt.points_b64); polys = b64u32(pt.polys_b64);
        if (pt.normals_b64) normals = b64f32(pt.normals_b64); }
      else { pts = b64f32(pt.positions_b64); const n = pts.length / 9;
        polys = new Uint32Array(n * 4);
        for (let t = 0; t < n; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = t * 3; polys[t * 4 + 2] = t * 3 + 1; polys[t * 4 + 3] = t * 3 + 2; } }
      const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
      if (normals && normals.length === pts.length) {
        pd.getPointData().setNormals(vtk.Common.Core.vtkDataArray.newInstance({ name: "Normals", values: normals, numberOfComponents: 3 }));
      }
      const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
      const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper); actor.setPickable(true);
      const pr = actor.getProperty();
      // a thermal model's placed parts each carry a colour of their own; any other skin is one body
      const col = Array.isArray(pt.color) && pt.color.length === 3 ? pt.color : BODY;
      pr.setColor(col[0], col[1], col[2]); pr.setEdgeVisibility(false);
      pr.setInterpolationToPhong();
      pr.setAmbient(0.32); pr.setDiffuse(0.76); pr.setSpecular(0.12); pr.setSpecularPower(20);
      ren.addActor(actor);
      skins.push({ actor, pd, pts, polys });
    });
    drawnNames = (s.patches || []).map((pt) => String(pt.name || ""));
    /* THE PARTS - the file's own solids, each under its own name and colour: the legend names
       each one. A thermal model's placed parts have always been drawn this way; a STEP assembly
       is drawn this way too, from the same payload (cad/solid_parts). */
    if (s.parts) { legendEl = partsLegend(s.patches || []); host.appendChild(legendEl); }
    /* THE SHARP EDGES - dark hairlines where faces meet at an angle, and along open rims */
    edgeCount = 0;
    if (s.edges && s.edges.lines_b64) {
      const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(b64f32(s.edges.points_b64), 3);
      const lines = b64u32(s.edges.lines_b64); pd.getLines().setData(lines); edgeCount = lines.length / 3;
      const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
      const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
      const pp = ac.getProperty(); pp.setColor(...EDGE); pp.setLineWidth(1); pp.setLighting(false); pp.setOpacity(0.9);
      ren.addActor(ac); edgeActor = ac;
    }
  }
  buildSkin(surf);
  const cam = ren.getActiveCamera();
  cam.azimuth(45); cam.elevation(25);
  ren.resetCamera(); rw.render();
  const bs = ren.computeVisiblePropBounds();
  const diag = Math.max(Math.hypot(bs[1] - bs[0], bs[3] - bs[2], bs[5] - bs[4]), 1e-9);
  // THE FIT IS THE PART'S. The far-field box, when drawn, is many part-lengths wide; framing on
  // it would leave the part a speck in the middle.
  const partBounds = bs.slice();
  const centre = [(bs[0] + bs[1]) / 2, (bs[2] + bs[3]) / 2, (bs[4] + bs[5]) / 2];

  // THE FRAMING FOLLOWS THE CANVAS. The workbench grid gives the canvas its size a tick after
  // the stage is appended, so the first fit is made against a placeholder box; every size change
  // until the user has moved the camera re-fits, and after that only "Fit" does.
  let touched = false;
  function fit() {
    ren.resetCamera(partBounds);
    // resetCamera fits the part to the view's HEIGHT; a canvas taller than it is wide (the
    // drawer open on a small window) would crop the sides, so back off by the aspect
    const [w, h] = apiRW.getSize(), a = w / Math.max(h, 1);
    if (a < 1) { cam.dolly(a); ren.resetCameraClippingRange(); }
    rw.render(); }
  /* THE OPENING VIEW: the drawing-office three-quarter view, framed on the part, with the axis the
     user says is up pointing up - the one they set for this check, else the one the check
     proposes (p.up_axis), else +Z */
  const upSel = box.querySelector("#gs-up-" + sessionId);
  let upAxis = (upSel && upSel.value) || "+z";
  function iso() {
    cam.setPosition(centre[0] - diag, centre[1] - diag, centre[2] + 0.8 * diag);
    cam.setFocalPoint(...centre); cam.setViewUp(0, 0, 1);
    orient(cam, upAxis, [-1, -1, 0.8]);
    fit();
  }
  const ro = new ResizeObserver(() => { setRenderScale(); if (!touched) fit(); else rw.render(); });
  ro.observe(host);
  host.addEventListener("pointerdown", (e) => { if (!(e.target.closest && e.target.closest(".v-tools"))) touched = true; }, true);
  const tools = box.querySelector(".v-tools");
  shield(tools);
  if (upSel) upSel.onchange = () => { upAxis = upSel.value; saveUp("session", sessionId, upAxis); iso(); };
  const fitBtn = box.querySelector("#gs-fit-" + sessionId);
  if (fitBtn) fitBtn.onclick = () => iso();

  /* THE STICKERS - a ball on every opening, at the position the check measured, and an HTML pin
     with its number that follows it every frame. The pin is the thing to click: it is never
     hidden by the part, only dimmed when its mouth faces away from the camera. */
  const pins = [];
  let sel = null;
  const panel = box.querySelector(".gc-panel");
  /* READ AGAIN, NOT HELD FOR GOOD (see `rebind`): the whole panel is rewritten when the route lands
     as "fea" and the FEA panel is built in place, so a reference taken at the stage's first draw
     would go on answering about a detached node. */
  let form = panel.querySelector(".gc-form");
  function centreOf(o) {
    return o.centroid_m && o.centroid_m.length === 3 ? o.centroid_m : (o.centroid_mm || [0, 0, 0]).map((v) => v / 1000);
  }
  function addPin(o) {
    const c = centreOf(o), n = o.normal || [0, 0, 1];
    const mp = vtk.Rendering.Core.vtkMapper.newInstance();
    mp.setInputData(spherePd(c[0], c[1], c[2], 0.012 * diag));
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(STICKER[0], STICKER[1], STICKER[2]);
    pp.setAmbient(0.55); pp.setDiffuse(0.6); pp.setSpecular(0.2);
    ren.addActor(ac);
    const el = document.createElement("div"); el.className = "v-pin gc-pin"; el.textContent = String(o.id);
    el.title = "opening " + o.id + " - click to select";
    el.onclick = (ev) => { ev.stopPropagation(); select(o.id); };
    host.appendChild(el);
    pins.push({ id: o.id, o: { id: o.id, c, n }, actor: ac, el });
  }
  function removePin(id) {
    const k = pins.findIndex((pn) => pn.id === id); if (k < 0) return;
    ren.removeActor(pins[k].actor); pins[k].el.remove(); pins.splice(k, 1);
    if (sel === id) sel = null;
    rw.render();
  }
  function rows() { return [...form.querySelectorAll(".gc-table tbody tr")]; }
  function select(id) {
    sel = sel === id ? null : id;
    pins.forEach((pn) => { const on = pn.id === sel;
      const pp = pn.actor.getProperty();
      pp.setColor(on ? SEL[0] : STICKER[0], on ? SEL[1] : STICKER[1], on ? SEL[2] : STICKER[2]);
      pn.el.classList.toggle("sel", on); });
    rows().forEach((tr) => { const on = Number(tr.dataset.id) === sel; tr.classList.toggle("sel", on);
      if (on) { tr.scrollIntoView({ block: "nearest" }); const inp = tr.querySelector(".gc-name"); if (inp && !inp.disabled) inp.focus({ preventScroll: true }); } });
    rw.render();
  }
  /* AN FEA PART'S HOT SPOTS (the server's fea.measure, FEA build step 3): a small ball on every
     hole, fillet, notch and sharp inside corner the mesh will be refined at, and a tag with its
     number that follows it every frame. Shown only - version 1 refines them all automatically, so
     a tag lights its line in the panel and nothing more. */
  let spots = [];
  /* EVERY hot spot gets its mark: past SPOT_TAGS of them a numbered tag on each would bury the part,
     so each is a small mark without a number (its number shows when it is pointed at or chosen) */
  const SPOT_TAGS = 40;
  let spotMarks = false;
  function setSpots(list) {
    spots.forEach((s) => { ren.removeActor(s.actor); s.el.remove(); });
    spots = [];
    spotMarks = (list || []).length > SPOT_TAGS;
    (list || []).forEach(addSpot);
    bindMeasure(panel, lightSpot);
    rw.render();
  }
  function addSpot(h) {
    if (!h.at_mm || h.at_mm.length !== 3) return;
    const c = h.at_mm.map((v) => v / 1000);
    const mp = vtk.Rendering.Core.vtkMapper.newInstance();
    mp.setInputData(spherePd(c[0], c[1], c[2], 0.008 * diag));
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const col = h.type === "interface_overlap" ? OVERLAP : h.type === "interface_touching" ? TOUCH : SPOT;
    const pp = ac.getProperty(); pp.setColor(col[0], col[1], col[2]);
    pp.setAmbient(0.55); pp.setDiffuse(0.6); pp.setSpecular(0.2);
    ren.addActor(ac);
    const el = document.createElement("div");
    el.className = "v-pin fea-spot" + (String(h.type || "").startsWith("interface_") ? " fea-itf" : "") + (spotMarks ? " mark" : "");
    el.textContent = String(h.id); el.dataset.spot = String(h.id);
    el.title = `${h.id}: ${h.shape || h.type}${h.capped ? " (refinement capped)" : ""}`;
    el.onclick = (ev) => { ev.stopPropagation(); lightSpot(h.id); };
    host.appendChild(el);
    spots.push({ id: h.id, c, el, actor: ac });
  }
  function lightSpot(id) {
    spots.forEach((s) => s.el.classList.toggle("sel", s.id === id));
    lightSpotRow(panel, id);
  }
  if (p.hot_spots) setSpots(p.hot_spots);

  /* LOOK INTO A MOUTH - the close-up the pictures had, now a camera move: from outside, straight
     down the opening's normal, framed on it */
  function look(id) {
    const pn = pins.find((x) => x.id === id); if (!pn) return;
    const { c, n } = pn.o;
    cam.setFocalPoint(c[0], c[1], c[2]);
    cam.setPosition(c[0] + n[0] * 0.9 * diag, c[1] + n[1] * 0.9 * diag, c[2] + n[2] * 0.9 * diag);
    // the chosen up stays up, unless the mouth faces straight along it
    const u = upVector(upAxis), along = Math.abs(n[0] * u[0] + n[1] * u[1] + n[2] * u[2]);
    cam.setViewUp(...(along < 0.9 ? u : (Math.abs(u[2]) > 0.5 ? [0, 1, 0] : [0, 0, 1])));
    ren.resetCameraClippingRange(); cam.zoom(1.6); rw.render();
    touched = true;
    if (sel !== id) select(id);
  }

  /* ADDING A STICKER: click the part where the opening is. The click is a ray through the clicked
     pixel, read against the check's own holes (cad/open_ends on the server - the same holes the
     upload's stickers come from), in this order: the hole the ray looks into - through it, where
     there is no surface to click, or onto the end face around it - else the measured flat face
     the click lands on, else the hole nearest the spot clicked. Each carries a real centre, size
     and direction. When none is there the sticker is never dropped on the wall: the stage asks
     for the hole's edge instead - two more clicks on its rim - and the circle through the three
     points is the opening. Esc cancels. When the user says the file is the fluid volume itself,
     its mouths are faces, not holes, and only the measured faces are read. */
  let addMode = false, downXY = null;
  let trace = null;                                  // the rim points clicked so far, while tracing
  const traceDots = [];
  const ADD_HINT = "then click the part where it is";
  const TRACE_HINT = "Couldn't find a hole here: click 2 more points around its edge";
  function hint(text) { const th = form.querySelector(".gc-tools-hint"); if (th) th.textContent = text; }
  function setAddMode(on, said) {
    addMode = on;
    endTrace();
    if (on && measuring) setMeasure(false);
    const b = form.querySelector(".gc-add"); if (b) { b.classList.toggle("armed", on); b.textContent = on ? "Click the part…" : "Add an opening"; }
    host.style.cursor = on ? "crosshair" : "";
    hint(on ? "click the spot on the part where the opening is (dragging still turns it)" : (said || ADD_HINT));
    if (on) setTimeout(clicksNow, 30);               // read the skin while the user aims, not on the click
  }
  // what a click is read against: the skin's triangles and the check's holes, read once
  let clicks = null;
  function clicksNow() {
    if (!clicks) clicks = clickIndex(skins.map((s) => ({ pts: s.pts, polys: s.polys })), p.holes || []);
    return clicks;
  }
  /** The ray through a point on the canvas, from the camera into the scene, in the skin's units.
   *  It starts at the camera, not at the near clipping plane, which a camera moved without a
   *  re-fit can leave beyond the part. */
  function rayAt(clientX, clientY) {
    const rect = host.getBoundingClientRect(), size = apiRW.getSize();
    const x = (clientX - rect.left) / rect.width, y = 1 - (clientY - rect.top) / rect.height;
    const aspect = size[0] / Math.max(size[1], 1);
    const a = ren.normalizedDisplayToWorld(x, y, 0, aspect), b = ren.normalizedDisplayToWorld(x, y, 1, aspect);
    const dir = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], L = Math.hypot(dir[0], dir[1], dir[2]) || 1;
    const cp = cam.getPosition();
    const back = ((a[0] - cp[0]) * dir[0] + (a[1] - cp[1]) * dir[1] + (a[2] - cp[2]) * dir[2]) / L;
    return { origin: [a[0] - dir[0] / L * back, a[1] - dir[1] / L * back, a[2] - dir[2] / L * back], dir };
  }
  const r6 = (v) => Math.round(v * 1e6) / 1e6, r2 = (v) => Math.round(v * 100) / 100;
  /** The measured flat face that is this hole, when the measuring step measured it: the same
   *  direction, centre and size. A STEP file's faces are exact, so theirs are the numbers. */
  function measuredAs(h) {
    const n = h.normal, c = h.centroid_m, d = (h.diameter_mm || 0) / 1000;
    return (p.faces || []).find((f) => {
      const fn = f.normal || [0, 0, 1], fc = f.centroid_m || (f.centroid_mm || [0, 0, 0]).map((v) => v / 1000);
      const fd = (f.diameter_mm || 0) / 1000;
      return Math.abs(fn[0] * n[0] + fn[1] * n[1] + fn[2] * n[2]) >= 0.95 && Math.abs(fd - d) <= 0.1 * d
        && Math.hypot(fc[0] - c[0], fc[1] - c[1], fc[2] - c[2]) <= 0.1 * d;
    }) || null;
  }
  /** A new opening on a hole, as the check measured it: centre, size, which way it faces. */
  function addHole(h) {
    const f = measuredAs(h);
    if (f) return place(fromFace(f, { open_end: true }));
    const id = nextId();
    return place({ id, name: `opening_${id}`, role: "outlet", shape: h.shape, kind: h.kind, confidence: 0.5,
      centroid_m: h.centroid_m, centroid_mm: h.centroid_mm, normal: h.normal,
      diameter_mm: h.diameter_mm, width_mm: h.width_mm, height_mm: h.height_mm,
      // an annular mouth: the centre body standing in it, so the opening confirmed is the ring
      inner_diameter_mm: h.inner_diameter_mm, flow_area_mm2: h.flow_area_mm2,
      added: true, snapped: true, open_end: true });
  }
  /** What a click does, along the ray through the clicked pixel (see above): the opening it
   *  added, or null - nothing met, or the stage now asking for the hole's edge. */
  function addAt(ray) {
    const idx = clicksNow();
    if (trace) return traceAt(snapAt({ ...idx, holes: [] }, ray).hit, ray.origin);
    const fluid = (form.querySelector(".gc-kind") || {}).value === "fluid-domain";
    const s = snapAt(fluid ? { ...idx, holes: [] } : idx, ray);
    if (s.through) return done(addHole(s.through), "Snapped to the hole there.");
    if (!s.hit) return null;
    const face = faceAt(s.hit.point, s.hit.normal);
    if (face) return done(place(fromFace(face)), "");
    if (s.near) return done(addHole(s.near), "Snapped to the hole there.");
    trace = [];
    return traceAt(s.hit, ray.origin);
  }
  function done(o, said) { setAddMode(false, said); return o; }
  /** TRACING A HOLE the check did not find: each click is a point on its rim; at the third, the
   *  circle through them is the opening - its centre, its diameter, and the three points' plane
   *  turned to face the viewer, who clicked from outside the part. */
  function traceAt(hit, viewer) {
    if (!hit) return null;
    trace.push(hit.point);
    const dot = vtk.Rendering.Core.vtkActor.newInstance(), mp = vtk.Rendering.Core.vtkMapper.newInstance();
    mp.setInputData(spherePd(hit.point[0], hit.point[1], hit.point[2], 0.006 * diag));
    dot.setMapper(mp); dot.setPickable(false); dot.getProperty().setColor(SEL[0], SEL[1], SEL[2]);
    ren.addActor(dot); traceDots.push(dot); rw.render();
    if (trace.length < 3) {
      hint(trace.length === 1 ? TRACE_HINT : "click 1 more point around its edge (Esc cancels)");
      return null;
    }
    const c = circleThrough(trace[0], trace[1], trace[2], viewer);
    if (!c) {                                        // three points in a line make no circle
      trace.pop(); ren.removeActor(traceDots.pop()); rw.render();
      hint("Those points lie in a line: click another point around the edge");
      return null;
    }
    const id = nextId();
    const o = place({ id, name: `opening_${id}`, role: "outlet", shape: "circle", kind: "traced", confidence: 0.5,
      centroid_m: c.centre.map(r6), centroid_mm: c.centre.map((v) => r2(v * 1000)), normal: c.normal.map((v) => Math.round(v * 1e5) / 1e5),
      diameter_mm: r2(c.diameter * 1000), added: true, snapped: true, traced: true });
    return done(o, "Placed on the circle through your three points.");
  }
  function endTrace() {
    trace = null;
    if (traceDots.length) { traceDots.splice(0).forEach((a) => ren.removeActor(a)); rw.render(); }
  }
  function onKey(e) {
    if (e.key !== "Escape") return;
    if (addMode) setAddMode(false);
    if (measuring) setMeasure(false);
  }
  document.addEventListener("keydown", onKey);
  function nextId() { return Math.max(0, ...(p.openings || []).map((o) => Number(o.id) || 0)) + 1; }
  function fromFace(f, extra) {
    const id = nextId();
    return { id, name: `opening_${id}`, role: "outlet", shape: f.shape, kind: f.kind, confidence: 0.5,
      centroid_m: f.centroid_m, centroid_mm: f.centroid_mm, normal: f.normal,
      diameter_mm: f.diameter_mm, width_mm: f.width_mm, height_mm: f.height_mm,
      inner_diameter_mm: f.inner_diameter_mm, flow_area_mm2: f.flow_area_mm2,
      added: true, snapped: true, ...extra };
  }
  /** The measured flat face a point on the part lies on, or null. Of concentric faces - a coaxial
   *  fitting's inner and outer port share a centre - the smallest one that still reaches the
   *  point is the one clicked; a face turned away from the clicked surface is never it. */
  function faceAt(point, normal) {
    let best = null, bestHalf = Infinity, bestD = Infinity;
    (p.faces || []).forEach((f) => {
      const fn = f.normal || [0, 0, 1];
      if (normal && (fn[0] * normal[0] + fn[1] * normal[1] + fn[2] * normal[2]) < 0.5) return;
      const fc = f.centroid_m || (f.centroid_mm || [0, 0, 0]).map((v) => v / 1000);
      const half = Math.max(f.width_mm || 0, f.height_mm || 0, f.diameter_mm || 0) / 1000 / 2;
      const dd = Math.hypot(point[0] - fc[0], point[1] - fc[1], point[2] - fc[2]);
      if (dd > 1.1 * half + 0.003) return;                // the point is not on this face (a tenth of slack)
      if (half < bestHalf || (half === bestHalf && dd < bestD)) { best = f; bestHalf = half; bestD = dd; }
    });
    return best;
  }
  /** A new opening at a point on the part (the support hook's way in): on the measured flat face
   *  there, else the point itself, with its size left for the user to type. Returns the opening. */
  function add(point, normal) {
    const face = faceAt(point, normal);
    if (face) return place(fromFace(face));
    const id = nextId();
    return place({ id, name: `opening_${id}`, role: "outlet", shape: "unknown", kind: "picked", confidence: 0.3,
      centroid_m: point.slice(), centroid_mm: point.map((v) => r2(v * 1000)), normal: normal.slice(), added: true });
  }
  /** The new opening's row and pin, selected. */
  function place(o) {
    p.openings = [...(p.openings || []), o];
    const tbody = form.querySelector(".gc-table tbody");
    if (tbody) { tbody.insertAdjacentHTML("beforeend", rowHtml(o, p)); }
    else {
      // the first opening on a part that had none: the table takes the "no openings" note's
      // place, and the Add button beside it stays
      const table = `<table class="gc-table">${tableHead()}<tbody>${rowHtml(o, p)}</tbody></table>`;
      const note = form.querySelector(".gc-int .gc-note");
      if (note) note.outerHTML = table; else form.querySelector(".gc-int").insertAdjacentHTML("afterbegin", table);
    }
    addPin(o); rebind(); select(o.id); rw.render();
    return o;
  }
  function remove(id) {
    p.openings = (p.openings || []).filter((o) => Number(o.id) !== Number(id));
    form.querySelector(`.gc-table tr[data-id="${id}"]`)?.remove();
    removePin(Number(id));
  }
  host.addEventListener("pointerdown", (e) => { downXY = [e.clientX, e.clientY]; }, true);
  host.addEventListener("pointerup", (e) => {
    if (!(addMode || measuring) || !downXY) return;
    if (e.target && e.target.closest && e.target.closest(".gc-pin,.v-tools")) return;
    if (Math.hypot(e.clientX - downXY[0], e.clientY - downXY[1]) > 6) return;
    if (measuring) measureAt(e.clientX, e.clientY);
    else addAt(rayAt(e.clientX, e.clientY));
  }, true);

  /* MEASURING: two clicks on the part draw a thin line between them, a dot at each end and the
     distance beside it, in the unit the sizes are shown in. A click within a few pixels of a
     hole's edge lands on that edge, so a hole's width across is two clicks. A third click starts
     a new measurement; Esc or the button ends it. It sends nothing and changes nothing the user
     confirms - a ruler, not an answer. */
  let measuring = false;
  const measurePts = [], measureActors = [];
  let measureLabel = null;
  const RIM_SNAP_PX = 8;
  function setMeasure(on) {
    measuring = on;
    clearMeasure();
    if (on && addMode) setAddMode(false);
    const b = form.querySelector(".gc-measure");
    if (b) { b.classList.toggle("armed", on); b.textContent = on ? "Measuring…" : "Measure"; }
    host.style.cursor = on ? "crosshair" : "";
    hint(on ? "click two points on the part (a hole's edge pulls the click onto it); Esc ends" : ADD_HINT);
  }
  function clearMeasure() {
    measurePts.length = 0;
    measureActors.splice(0).forEach((a) => ren.removeActor(a));
    if (measureLabel) { measureLabel.remove(); measureLabel = null; }
    rw.render();
  }
  /** Whether a point is in front of the camera (whatever the clipping planes say). */
  function inFront(pt) {
    const cp = cam.getPosition(), dop = cam.getDirectionOfProjection();
    return (pt[0] - cp[0]) * dop[0] + (pt[1] - cp[1]) * dop[1] + (pt[2] - cp[2]) * dop[2] > 0;
  }
  /** Where a point of the part is on the screen, in client pixels; null when it is behind the camera. */
  function screenOf(pt) {
    if (!inFront(pt)) return null;
    const size = apiRW.getSize(), rect = host.getBoundingClientRect();
    const nd = ren.worldToNormalizedDisplay(pt[0], pt[1], pt[2], size[0] / size[1]);
    return [rect.left + nd[0] * rect.width, rect.top + (1 - nd[1]) * rect.height];
  }
  /** The hole edge point nearest a click on the screen, within RIM_SNAP_PX, that the camera can
   *  see (nothing of the part stands between them), or null. */
  function rimPointNear(clientX, clientY) {
    let best = null, bestD = RIM_SNAP_PX;
    for (const h of p.holes || []) {
      for (const pt of [...(h.loop_m || []), ...(h.outer_m || [])]) {
        const sp = screenOf(pt);
        if (!sp) continue;
        const dd = Math.hypot(sp[0] - clientX, sp[1] - clientY);
        if (dd < bestD) { best = pt; bestD = dd; }
      }
    }
    if (!best) return null;
    const cp = cam.getPosition(), dir = [best[0] - cp[0], best[1] - cp[1], best[2] - cp[2]];
    const L = Math.hypot(dir[0], dir[1], dir[2]);
    const hit = snapAt({ ...clicksNow(), holes: [] }, { origin: cp, dir }).hit;
    return hit && hit.t < L - 1e-4 * diag ? null : best;      // hidden behind the part: not it
  }
  /** One measuring click at a point on the canvas: the point it lands on (a hole's edge when one
   *  is that close), and - at the second - the line, the dots and the distance. */
  function measureAt(clientX, clientY) {
    const pt = rimPointNear(clientX, clientY) || (snapAt({ ...clicksNow(), holes: [] }, rayAt(clientX, clientY)).hit || {}).point;
    if (!pt) return null;
    if (measurePts.length >= 2) clearMeasure();               // a third click starts again
    measurePts.push(pt);
    const dotPd = spherePd(pt[0], pt[1], pt[2], 0.004 * diag);
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(dotPd);
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    ac.getProperty().setColor(SEL[0], SEL[1], SEL[2]); ac.getProperty().setLighting(false);
    ren.addActor(ac); measureActors.push(ac);
    if (measurePts.length === 2) {
      const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(Float32Array.from([...measurePts[0], ...measurePts[1]]), 3);
      pd.getLines().setData(Uint32Array.from([2, 0, 1]));
      const lm = vtk.Rendering.Core.vtkMapper.newInstance(); lm.setInputData(pd);
      const la = vtk.Rendering.Core.vtkActor.newInstance(); la.setMapper(lm); la.setPickable(false);
      const lp = la.getProperty(); lp.setColor(SEL[0], SEL[1], SEL[2]); lp.setLineWidth(1.5); lp.setLighting(false);
      ren.addActor(la); measureActors.push(la);
      measureLabel = document.createElement("div");
      measureLabel.className = "gc-mlabel";
      measureLabel.textContent = measuredNow().label;
      host.appendChild(measureLabel);
    }
    rw.render();
    return measuredNow();
  }
  /** The measurement as it stands: its points, its length in the reading's millimetres, and the
   *  words beside the line - the number as the sizes on the form show it, in the file's unit. */
  function measuredNow() {
    if (measurePts.length < 2) return { points: measurePts.slice(), mm: null, label: null };
    const [a, b] = measurePts;
    const mm = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]) * 1000;
    return { points: measurePts.slice(), mm, label: `${shown(mm, p)} ${unitOf(p)}` };
  }
  function placeMeasureLabel(aspect, rect) {
    if (!measureLabel || measurePts.length < 2) return;
    const [a, b] = measurePts, m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
    const nd = ren.worldToNormalizedDisplay(m[0], m[1], m[2], aspect);
    const ok = inFront(m);
    measureLabel.style.display = ok ? "" : "none";
    if (ok) { measureLabel.style.left = (nd[0] * rect.width) + "px"; measureLabel.style.top = ((1 - nd[1]) * rect.height) + "px"; }
  }

  /* the form is re-drawn when the model's labels arrive, and BUILT where a waiting note stood once
     the route lands, so its controls are re-bound then - and the section itself is read again,
     because the FEA panel replaces the whole panel rather than the form's contents */
  function rebind() {
    form = panel.querySelector(".gc-form") || form;
    rows().forEach((tr) => {
      const n = tr.querySelector(".gc-n"); if (n) { n.title = "click to look into this opening"; n.onclick = () => look(Number(tr.dataset.id)); }
      const x = tr.querySelector(".gc-x"); if (x) x.onclick = () => remove(Number(tr.dataset.id));
      if (!tr.dataset.bound) { tr.dataset.bound = "1"; tr.addEventListener("focusin", () => { const id = Number(tr.dataset.id); if (sel !== id) select(id); }); }
    });
    const addBtn = form.querySelector(".gc-add"); if (addBtn) addBtn.onclick = () => setAddMode(!addMode);
    const mBtn = form.querySelector(".gc-measure");
    if (mBtn) { mBtn.onclick = () => setMeasure(!measuring); mBtn.classList.toggle("armed", measuring); }
    // pins follow the rows: a row without a pin gets one, a pin without a row goes
    const ids = new Set(rows().map((tr) => Number(tr.dataset.id)));
    pins.slice().forEach((pn) => { if (!ids.has(pn.id)) removePin(pn.id); });
    (p.openings || []).forEach((o) => { if (ids.has(Number(o.id)) && !pins.some((pn) => pn.id === Number(o.id))) addPin(o); });
  }
  rebind();

  /* A BODY IN A FLOW: an arrow for the way the fluid travels and the far-field box the mesh
     will be built in, both drawn from the form and redrawn as the user changes it. */
  let decor = [];
  function clearDecor() { decor.forEach((a) => ren.removeActor(a)); decor = []; }
  function lineActor(pts, lines, color, width) {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts), 3); pd.getLines().setData(Uint32Array.from(lines));
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(...color); pp.setLineWidth(width); pp.setLighting(false);
    ren.addActor(ac); decor.push(ac); return ac;
  }
  function refreshExternal() {
    clearDecor();
    const flowSel = form.querySelector(".gc-flow");
    const external = !!flowSel && flowSel.value === "external";
    pins.forEach((pn) => { pn.actor.setVisibility(!external); if (external) pn.el.style.display = "none"; });
    if (!external) { rw.render(); return; }
    const ex = readExternal(form, p);
    const axis = ex.flow_axis && ex.flow_axis !== "unknown" ? ex.flow_axis : "+x";
    const k = { x: 0, y: 1, z: 2 }[axis[1]], sign = axis[0] === "-" ? -1 : 1;
    const dir = [0, 0, 0]; dir[k] = sign;
    const size = [partBounds[1] - partBounds[0], partBounds[3] - partBounds[2], partBounds[5] - partBounds[4]];
    const L = (ex.reference_length_mm || 0) > 0 ? ex.reference_length_mm / 1000 : (size[k] || diag);
    const c = centre;
    const half = 0.7 * Math.max(size[k], 0.3 * diag);
    const tail = c.map((v, i) => v - dir[i] * half), tip = c.map((v, i) => v + dir[i] * half);
    const u = k === 2 ? [1, 0, 0] : [0, 0, 1], w = [dir[1] * u[2] - dir[2] * u[1], dir[2] * u[0] - dir[0] * u[2], dir[0] * u[1] - dir[1] * u[0]];
    const h = 0.12 * half;
    const back = tip.map((v, i) => v - dir[i] * h * 2);
    const pts = [...tail, ...tip, ...back.map((v, i) => v + u[i] * h), ...back.map((v, i) => v - u[i] * h),
                 ...back.map((v, i) => v + w[i] * h), ...back.map((v, i) => v - w[i] * h)];
    lineActor(pts, [2, 0, 1, 2, 1, 2, 2, 1, 3, 2, 1, 4, 2, 1, 5], SEL, 3);
    const e = ex.extents || {};
    const lo = partBounds.filter((_, i) => i % 2 === 0), hi = partBounds.filter((_, i) => i % 2 === 1);
    const up = (sign > 0 ? e.upstream : e.downstream) || 5, down = (sign > 0 ? e.downstream : e.upstream) || 5;
    lo[k] -= up * L; hi[k] += down * L;
    const vert = k === 2 ? 1 : 2, side = [0, 1, 2].find((i) => i !== k && i !== vert);
    lo[side] -= (e.lateral || 5) * L; hi[side] += (e.lateral || 5) * L;
    hi[vert] += (e.vertical || 5) * L;
    if (!ex.grounded) lo[vert] -= (e.vertical || 5) * L;
    const P = [[lo[0], lo[1], lo[2]], [hi[0], lo[1], lo[2]], [hi[0], hi[1], lo[2]], [lo[0], hi[1], lo[2]],
               [lo[0], lo[1], hi[2]], [hi[0], lo[1], hi[2]], [hi[0], hi[1], hi[2]], [lo[0], hi[1], hi[2]]];
    const E = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
    lineActor(P.flat(), E.flatMap(([a, b]) => [2, a, b]), [0.45, 0.6, 0.8], 1);
    if (ex.grounded) {
      const g = lo[vert], Q = P.filter((q) => q[vert] === g);
      lineActor(Q.flat(), [2, 0, 2, 2, 1, 3], [0.45, 0.6, 0.8], 1);
    }
    rw.render();
  }
  panel.addEventListener("change", (ev) => { if (ev.target.closest(".gc-flow,.gc-axis,.gc-ground,.gc-ext")) refreshExternal(); });
  panel.addEventListener("input", (ev) => { if (ev.target.closest(".gc-ref,.gc-extents")) refreshExternal(); });
  refreshExternal();

  /* THE AXES - bottom right, the X, Y and Z of the part turning with it, so the user always
     knows which way the part's coordinates run. Drawn as a small overlay from the camera's
     frame: an axis pointing away from the viewer is dimmed. */
  const axesEl = box.querySelector(".gc-axes");
  const axesLines = axesEl ? ["x", "y", "z"].map((k) => [axesEl.querySelector("line.ax-" + k), axesEl.querySelector("text.ax-" + k)]) : [];
  let axesNow = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] };
  function drawAxes() {
    if (!axesEl) return;
    const dir = cam.getDirectionOfProjection(), up = cam.getViewUp();
    const right = [dir[1] * up[2] - dir[2] * up[1], dir[2] * up[0] - dir[0] * up[2], dir[0] * up[1] - dir[1] * up[0]];
    const rl = Math.hypot(right[0], right[1], right[2]) || 1;
    right[0] /= rl; right[1] /= rl; right[2] /= rl;
    const upp = [right[1] * dir[2] - right[2] * dir[1], right[2] * dir[0] - right[0] * dir[2], right[0] * dir[1] - right[1] * dir[0]];
    const C = 42, L = 28;
    axesNow = {};
    ["x", "y", "z"].forEach((k, i) => {
      // the world axis i on the screen: its parts along the camera's right, up and out
      const sx = right[i], sy = upp[i], sz = -dir[i];
      axesNow[k] = [sx, sy, sz];
      const [line, label] = axesLines[i];
      line.setAttribute("x2", (C + L * sx).toFixed(1)); line.setAttribute("y2", (C - L * sy).toFixed(1));
      label.setAttribute("x", (C + (L + 9) * sx).toFixed(1)); label.setAttribute("y", (C - (L + 9) * sy).toFixed(1));
      line.classList.toggle("back", sz < 0); label.classList.toggle("back", sz < 0);
    });
  }

  let alive = true;
  (function pinLoop() {
    if (!alive || !document.body.contains(host)) return;
    drawAxes();
    const size = apiRW.getSize(), aspect = size[0] / size[1], rect = host.getBoundingClientRect();
    const cp = cam.getPosition();
    const externalNow = (form.querySelector(".gc-flow") || {}).value === "external";
    pins.forEach((pn) => {
      if (externalNow) { pn.el.style.display = "none"; return; }
      const [x, y, z] = pn.o.c;
      const nd = ren.worldToNormalizedDisplay(x, y, z, aspect);
      const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
      pn.el.style.display = ok ? "" : "none";
      if (!ok) return;
      pn.el.style.left = (nd[0] * rect.width) + "px";
      pn.el.style.top = ((1 - nd[1]) * rect.height) + "px";
      const n = pn.o.n, facing = (n[0] * (x - cp[0]) + n[1] * (y - cp[1]) + n[2] * (z - cp[2])) < 0;
      pn.el.classList.toggle("back", !facing);
    });
    spots.forEach((s) => {
      const nd = ren.worldToNormalizedDisplay(s.c[0], s.c[1], s.c[2], aspect);
      const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
      s.el.style.display = ok ? "" : "none";
      if (!ok) return;
      s.el.style.left = (nd[0] * rect.width) + "px";
      s.el.style.top = ((1 - nd[1]) * rect.height) + "px";
    });
    placeMeasureLabel(aspect, rect);
    requestAnimationFrame(pinLoop);
  })();

  setRenderScale(); iso();

  /** THE SAME PART, DRAWN BETTER. A big STEP opens the stage on the quick preview, whose skin is
   *  one welded lump; the full reading replaces the stored skin with the file's own named solids
   *  (geometry_check._parts_skin), and the stage fetched the skin exactly ONCE - so a six-solid
   *  cold plate stayed one grey shape for the whole session, which is the first thing a
   *  semiconductor customer checks. This swaps the drawing in place.
   *
   *  The camera, the stickers and the form do not move: it is the same part at the same
   *  tessellation (measured on a three-box STEP: 36 triangles both ways), so the bounds the view
   *  was framed on and the sticker radius taken from them still hold. Only the click index is
   *  thrown away, because it is built from the triangles. */
  function setSkin(s) {
    if (!s || !(s.patches || []).length) return;
    skins.splice(0).forEach((k) => ren.removeActor(k.actor));
    if (edgeActor) { ren.removeActor(edgeActor); edgeActor = null; }
    if (legendEl) { legendEl.remove(); legendEl = null; }
    clicks = null;
    buildSkin(s);
    rw.render();
  }

  /* support/debug hook - the browser tier drives the stage through it, without pixel picking.
     CLEARED BY IDENTITY: `stop()` removes this key only while it is still this scene's own hook, so
     a scene stopped after another has taken the key cannot delete the live one's. One session now
     opens one stage (composer.mountStage) and one scene, so nothing is expected to share the key -
     but an unconditional delete would be a defect waiting for the next thing that does. */
  window._vdbg = window._vdbg || {};
  const hook = {
    pins: () => pins.length, spots: () => spots.map((s) => s.id), selected: () => sel, select, look, diag, add, addAt, rayAt, remove, iso,
    tracing: () => (trace ? trace.length : null), adding: () => addMode,
    measuring: () => measuring, measured: () => measuredNow(), measureAt, screenOf,
    openings: () => (p.openings || []).map((o) => Number(o.id)),
    external: () => ({ arrow: decor.length >= 1, box: decor.length >= 2, actors: decor.length }),
    visible: () => pins.filter((pn) => pn.el.style.display !== "none").length,
    camera: () => cam.getPosition(), cam: () => cam, edges: () => edgeCount, axes: () => { drawAxes(); return axesNow; },
    smooth: () => skins.some((s) => !!s.pd.getPointData().getNormals()),
    upAxis: () => upAxis, setUp: (a) => { if (upSel) { upSel.value = a; upSel.onchange(); } },
    render: () => rw.render(),
    // what the part is drawn as now: one body, or the file's own named solids
    patches: () => skins.length, partNames: () => drawnNames.slice(),
  };
  window._vdbg["gstage:" + sessionId] = hook;
  return {
    stop: () => {
      alive = false; ro.disconnect(); clearDecor(); clearMeasure();
      document.removeEventListener("keydown", onKey);
      if (window._vdbg["gstage:" + sessionId] === hook) delete window._vdbg["gstage:" + sessionId];
      /* THE RENDER WINDOW IS TORN DOWN WITH THE SCENE: stop() used to leave vtk.js's whole object
         graph alive behind a removed box. It does NOT hand the browser's context back - MEASURED in
         Chrome, after both deletes, on the released stage's own canvas: isContextLost() false,
         getParameter(VERSION) still "WebGL 2.0", createProgram() still succeeds. Only
         WEBGL_lose_context can release one and nothing in this console does that anywhere, so what
         holds the count down is that a session opens ONE scene (see the test that counts them, and
         composer.mountStage). Wrapped because a vtk.js build that cannot free it must not stop a
         stage from being released. */
      try { apiRW.delete(); } catch { /* already gone */ }
      try { grw.delete(); } catch { /* already gone */ }
    },
    rebind, refresh: refreshExternal, setSpots, setSkin,
  };
}

/** The part's name and its size - measured, and true before anything has been asked about it. */
function waitingLeadHtml(p) {
  const size = (p.size_mm || []).map((v) => shown(v, p)).join(" x ");
  return `<div class="gc-part"><b>${esc(p.part || p.part_name || "your part")}</b>${size ? `<span class="gc-size">${esc(size)} <span class="gc-u">${unitOf(p)}</span></span>` : ""}</div>
    <p>This is your file as Hexera read it. Turn it and check it is the right part.</p>`;
}

/* ======================================================================================
 * THE FEA PART STAGE - a part for structural analysis, shown as itself.
 *
 * The part in 3D, drawn as the flow stage draws it but with no stickers, no flow arrow and no far
 * field; beside it its size in the file's unit, the unit box, how many bodies the file has and
 * what the clean check found, and the one question: "Is this your part and are the units right?"
 * A file of several bodies is an assembly: its parts are listed, the places they touch are marked,
 * and it asks how the parts are connected (bonded or contact). Once the part
 * is measured, its thinnest wall and its hot spots (holes, fillets, notches, sharp inside corners)
 * are listed and tagged on the part - shown only, as version 1 refines them all automatically.
 * ====================================================================================== */
/** THE FEA PANEL, written into the stage's panel and bound, and the update() that follows the
 *  check on it. ONE DEFINITION, because there are two ways onto it: a part whose route was already
 *  "fea" when the stage opened, and a part whose stage opened before the route was known and grew
 *  this panel when "fea" landed. `scene()` gives the live scene rather than the scene - on the
 *  first way in the panel is bound BEFORE the skin has loaded, so that there is a Proceed to press
 *  while a big file is still coming down, and there is no scene yet. `done()` runs once the server
 *  holds the answer. */
function feaPanel(box, panel, f, p, { confirm, done, scene }) {
  // the scene draws stickers from the proposal's openings: an FEA part has none, but it has the
  // hot spots its measurement found (shown, never edited)
  p.openings = [];
  p.hot_spots = [...((f.measure && f.measure.hot_spots) || []), ...interfaceMarks(f.assembly)];
  box.classList.add("fea-stage");
  panel.classList.add("fea-panel");
  panel.innerHTML = feaPanelHtml(f, p);
  bindFeaPanel(panel, f, p, { confirm, done });
  // a unit settled in the chat reaches an untouched box; nothing else moves
  return (d2) => {
    const q = (d2 && d2.proposal) || {};
    followUnit(panel, p, q.unit, q.unit_basis);
    // THE MEASUREMENT CAN LAND AFTER THE STAGE OPENED (the check was still naming): its lines and
    // tags are drawn then
    const m = d2 && d2.fea && d2.fea.measure;
    const a = d2 && d2.fea && d2.fea.assembly;
    // the bodies, the clean check and whether it can go on, when the full reading lands after a
    // preview opened the stage: Proceed reads the same view (`f`), so a stop found late is shown
    // with its way on rather than met as a refusal
    if (d2 && d2.fea) { Object.assign(f, d2.fea); followClean(panel, f); }
    const moved = (m && followMeasure(panel, m, p, null)) | (a && followAssembly(panel, a));
    if (moved) {
      p.hot_spots = [...(((m || f.measure) || {}).hot_spots || []), ...interfaceMarks(a || f.assembly)];
      const s = scene();
      if (s) s.setSpots(p.hot_spots);
    }
  };
}

async function openFeaStage(sessionId, d, confirm, opts) {
  const p = { ...(d.proposal || {}) };
  const box = stageBox(sessionId, p, "");
  const release = takeOver(box, opts.anchorEl);
  const panel = box.querySelector(".gc-panel");
  let scene = null, closed = false;
  const follow = feaPanel(box, panel, d.fea || {}, p, {
    confirm, scene: () => scene,
    done: () => { closed = true; if (scene) scene.stop(); release(); },
  });
  try {
    await loadVtk();
    const surf = await getGeometrySkin(sessionId);
    if (closed) return null;
    scene = initScene(sessionId, box, surf, p);
    document.getElementById("gs-load-" + sessionId)?.remove();
  } catch (e) {
    if (closed) return null;
    console.warn("FEA stage unavailable, showing the card instead:", e && e.message);
    release();
    if (opts.fallback) opts.fallback();
    return null;
  }
  // an FEA ASSEMBLY is drawn as its solids too, and opens on the preview like any other big file
  const followSkin = skinFollower(sessionId, d, () => scene);
  return {
    release: () => { if (scene) scene.stop(); release(); },
    update: (d2) => { if (!d2) return; followSkin(d2); follow(d2); },
    hasForm: () => true,
    isNaming: () => false,
  };
}

/* ======================================================================================
 * THE THERMAL MODEL STAGE - an ECXML upload, shown the way a thermal tool opens a model.
 *
 * The file already says what every part is, what it is made of, how much heat it gives off and
 * where the fans and vents sit, so there is nothing here to name, no openings, no flow arrow and
 * no far-field box. The parts are drawn in the colours the panel's list wears; clicking a row (or
 * a part) lights that part and fades the rest, hovering says what it is, the case can be seen
 * through, and the air box the file sets is drawn as faint lines. The one answer is the detail
 * level, sent with "Mesh this model".
 * ====================================================================================== */
const AIRBOX = [0.43, 0.55, 0.69], OUTLINE = [1.0, 0.31, 0.0];
const FADED = 0.12, XRAY = 0.18;

async function openThermalStage(sessionId, d, confirm, opts) {
  const t = d.thermal || {};
  const id = "gstage-" + sessionId;
  document.getElementById(id)?.remove();
  const box = document.createElement("div"); box.className = "viewer gc-stage tm-stage"; box.id = id;
  const hasCase = (t.tree || []).some((r) => r.kind === "enclosure" && r.region);
  box.innerHTML = `<div class="v-bar gc-panel tm-panel">${thermalPanelHtml(t)}</div>
    <div class="v-canvas gc-canvas" id="gs-canvas-${sessionId}">
      <div class="v-loading" id="gs-load-${sessionId}"><div>Loading the model…</div></div>
      <div class="v-tools">${upSelectHtml("gs-up-" + sessionId, savedUp("session", sessionId) || "+z")}${hasCase ? `<button class="v-btn v-tool on" id="gs-xray-${sessionId}" type="button" aria-pressed="true" title="See through the case to the parts inside">See inside</button>` : ""}<button class="v-btn v-tool" id="gs-all-${sessionId}" type="button" hidden title="Show every part again">Show all</button><button class="v-btn v-tool" id="gs-fit-${sessionId}" type="button" title="Frame the whole model">Fit</button></div>
      <div class="tm-tip" hidden></div>
      <svg class="gc-axes" viewBox="0 0 84 84" aria-hidden="true">
        ${["x", "y", "z"].map((k) => `<line class="ax ax-${k}" x1="42" y1="42" x2="42" y2="42"/><text class="ax-l ax-${k}" x="42" y="42">${k.toUpperCase()}</text>`).join("")}
      </svg>
    </div>`;
  const release = takeOver(box, opts.anchorEl);
  const panelEl = box.querySelector(".tm-panel");
  let scene = null, closed = false;
  const panel = bindThermalPanel(panelEl, t, {
    select: (region) => scene && scene.select(region),
    hover: (region) => scene && scene.hover(region),
    frame: (region) => scene && scene.frame(region),
    probe: (name) => scene && scene.probe(name),
    confirm: async (body) => {
      await confirm(body, "");
      closed = true;
      if (scene) scene.stop();
      release();                         // the workbench goes back; the conversation carries on
    },
  });
  try {
    await loadVtk();
    const surf = await getGeometrySkin(sessionId);
    if (closed) return null;             // confirmed while the model loaded: nothing left to draw
    scene = initThermalScene(sessionId, box, surf, t, panel);
    document.getElementById("gs-load-" + sessionId)?.remove();
  } catch (e) {
    // the panel works without the picture: the card carries it instead
    if (closed) return null;             // confirmed already: no card for a model that is done
    console.warn("thermal stage unavailable, showing the card instead:", e && e.message);
    release();
    if (opts.fallback) opts.fallback();
    return null;
  }
  return {
    release: () => { if (scene) scene.stop(); release(); },
    // the file's model does not move while the page waits: nothing to follow
    update: () => {},
    hasForm: () => true,
    isNaming: () => false,
  };
}

function initThermalScene(sessionId, box, surf, t, panel) {
  const host = document.getElementById("gs-canvas-" + sessionId);
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: BACKGROUND });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  function setRenderScale() { const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR))); }
  grw.resize();
  host.addEventListener("contextmenu", (e) => e.preventDefault());
  bindMouse(grw);
  if (vtk.Rendering.Core.vtkLight) {
    [[-0.7, 0.9, 1.0, 0.62], [0.9, 0.25, 0.7, 0.28], [0.0, -0.4, -1.0, 0.14]].forEach(([x, y, z, i]) => {
      const l = vtk.Rendering.Core.vtkLight.newInstance();
      if (l.setLightTypeToCameraLight) l.setLightTypeToCameraLight();
      l.setPosition(x, y, z); l.setFocalPoint(0, 0, 0); l.setIntensity(i); l.setColor(1, 1, 1);
      ren.addLight(l);
    });
  }
  const rows = new Map((t.tree || []).filter((r) => r.region).map((r) => [r.region, r]));

  /* THE PARTS - one actor each, under the region name the list's rows carry */
  const parts = new Map();
  (surf.patches || []).forEach((pt) => {
    let pts, polys, normals = null;
    if (pt.polys_b64) { pts = b64f32(pt.points_b64); polys = b64u32(pt.polys_b64);
      if (pt.normals_b64) normals = b64f32(pt.normals_b64); }
    else { pts = b64f32(pt.positions_b64); const n = pts.length / 9;
      polys = new Uint32Array(n * 4);
      for (let q = 0; q < n; q++) { polys[q * 4] = 3; polys[q * 4 + 1] = q * 3; polys[q * 4 + 2] = q * 3 + 1; polys[q * 4 + 3] = q * 3 + 2; } }
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
    if (normals && normals.length === pts.length) {
      pd.getPointData().setNormals(vtk.Common.Core.vtkDataArray.newInstance({ name: "Normals", values: normals, numberOfComponents: 3 }));
    }
    const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
    const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper);
    const row = rows.get(pt.name) || {};
    // the list's colour is the part's colour: the same table on both sides (ecxml_place.PART_COLOURS)
    const col = Array.isArray(row.color) ? row.color : Array.isArray(pt.color) ? pt.color : BODY;
    const pr = actor.getProperty();
    pr.setColor(col[0], col[1], col[2]); pr.setInterpolationToPhong();
    pr.setAmbient(0.32); pr.setDiffuse(0.76); pr.setSpecular(0.12); pr.setSpecularPower(20);
    ren.addActor(actor);
    parts.set(pt.name, { actor, pd, pts, polys, index: null, isCase: (row.kind || "") === "enclosure" || pt.kind === "enclosure", row });
  });
  let edges = null;
  if (surf.edges && surf.edges.lines_b64) {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(b64f32(surf.edges.points_b64), 3); pd.getLines().setData(b64u32(surf.edges.lines_b64));
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    edges = vtk.Rendering.Core.vtkActor.newInstance(); edges.setMapper(mp); edges.setPickable(false);
    const pp = edges.getProperty(); pp.setColor(...EDGE); pp.setLineWidth(1); pp.setLighting(false); pp.setOpacity(0.9);
    ren.addActor(edges);
  }
  const cam = ren.getActiveCamera();
  ren.resetCamera(); rw.render();
  const bs = ren.computeVisiblePropBounds();
  const partBounds = bs.slice();
  const diag = Math.max(Math.hypot(bs[1] - bs[0], bs[3] - bs[2], bs[5] - bs[4]), 1e-9);
  const centre = [(bs[0] + bs[1]) / 2, (bs[2] + bs[3]) / 2, (bs[4] + bs[5]) / 2];

  function lines(P, E, color, width, opacity) {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(P.flat()), 3);
    pd.getLines().setData(Uint32Array.from(E.flatMap(([a, b]) => [2, a, b])));
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(...color); pp.setLineWidth(width); pp.setLighting(false); pp.setOpacity(opacity);
    ren.addActor(ac); return ac;
  }
  const BOX_E = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
  function boxPts(b) {
    return [[b[0], b[2], b[4]], [b[1], b[2], b[4]], [b[1], b[3], b[4]], [b[0], b[3], b[4]],
            [b[0], b[2], b[5]], [b[1], b[2], b[5]], [b[1], b[3], b[5]], [b[0], b[3], b[5]]];
  }
  /* THE AIR BOX the file sets: faint lines only, so it frames the model without hiding it. A box
     the file does not set (Hexera's box around the parts) is not drawn: there is no room air. */
  const dom = t.domain || {};
  let airbox = null;
  if (dom.source === "solutionDomain" && dom.box_mm && dom.box_mm.min && dom.box_mm.max) {
    const lo = dom.box_mm.min.map((v) => v / 1000), hi = dom.box_mm.max.map((v) => v / 1000);
    airbox = lines(boxPts([lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]), BOX_E, AIRBOX, 1, 0.55);
  }

  /* SEE INSIDE: a case is drawn see-through, so the board and the chips in it show, and a click
     goes through it to them. Its own row still lights it fully. */
  let xray = [...parts.values()].some((p) => p.isCase);
  // `selected`: the one part picked; `lit`: every part shown (that part, or a whole group)
  let selected = null, lit = null, hovered = null, outline = null, touched = false;
  function paint() {
    parts.forEach((p, region) => {
      const pr = p.actor.getProperty();
      let op = 1;
      // a faded case is fainter still: its walls are many layers deep between the eye and the part
      if (lit) op = lit.has(region) ? 1 : (p.isCase ? FADED / 2.5 : FADED);
      else if (xray && p.isCase) op = XRAY;
      pr.setOpacity(op);
      pr.setAmbient(region === hovered || region === selected ? 0.55 : 0.32);
      p.actor.setPickable(op >= 0.5);
    });
    if (edges) edges.getProperty().setOpacity(lit ? 0.12 : (xray ? 0.55 : 0.9));
    if (outline) { ren.removeActor(outline); outline = null; }
    if (lit) {
      // one box around what is shown: the part, or the whole group
      const b = [Infinity, -Infinity, Infinity, -Infinity, Infinity, -Infinity];
      lit.forEach((r) => { const q = parts.get(r).pd.getBounds();
        for (let i = 0; i < 6; i += 2) { b[i] = Math.min(b[i], q[i]); b[i + 1] = Math.max(b[i + 1], q[i + 1]); } });
      const pad = 0.004 * diag;
      outline = lines(boxPts([b[0] - pad, b[1] + pad, b[2] - pad, b[3] + pad, b[4] - pad, b[5] + pad]), BOX_E, OUTLINE, 1.5, 1);
    }
    const all = box.querySelector("#gs-all-" + sessionId); if (all) all.hidden = !lit;
    rw.render();
  }
  /** Light one part (a region name), a group of them (a list), or none (null). */
  function select(what) {
    const list = (Array.isArray(what) ? what : what ? [what] : []).filter((r) => parts.has(r));
    selected = !Array.isArray(what) && list.length ? list[0] : null;
    lit = list.length ? new Set(list) : null;
    paint();
  }
  function hover(region) { hovered = region && parts.has(region) ? region : null; paint(); }
  function frame(region) {
    const p = parts.get(region); if (!p) return;
    select(region); panel.select(region);
    ren.resetCamera(p.pd.getBounds()); cam.zoom(0.7); ren.resetCameraClippingRange(); rw.render();
    touched = true;
  }

  /* PICKING: the ray through the pixel, against each part the user can see (a faded or
     see-through part lets the click through). Parts are boxes and cylinders: a plain ray cast. */
  function rayAt(clientX, clientY) {
    const rect = host.getBoundingClientRect(), size = apiRW.getSize();
    const x = (clientX - rect.left) / rect.width, y = 1 - (clientY - rect.top) / rect.height;
    const aspect = size[0] / Math.max(size[1], 1);
    const a = ren.normalizedDisplayToWorld(x, y, 0, aspect), b = ren.normalizedDisplayToWorld(x, y, 1, aspect);
    const dir = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], L = Math.hypot(dir[0], dir[1], dir[2]) || 1;
    const cp = cam.getPosition();
    const back = ((a[0] - cp[0]) * dir[0] + (a[1] - cp[1]) * dir[1] + (a[2] - cp[2]) * dir[2]) / L;
    return { origin: [a[0] - dir[0] / L * back, a[1] - dir[1] / L * back, a[2] - dir[2] / L * back],
             dir: [dir[0] / L, dir[1] / L, dir[2] / L] };
  }
  function pickAt(clientX, clientY) {
    const ray = rayAt(clientX, clientY);
    let best = null, bestT = Infinity;
    parts.forEach((p, region) => {
      if (!p.actor.getPickable()) return;
      if (!p.index) p.index = clickIndex([{ pts: p.pts, polys: p.polys }], []);
      const hit = firstHit(p.index, ray.origin, ray.dir);
      if (hit && hit.t < bestT) { bestT = hit.t; best = region; }
    });
    return best;
  }
  const tip = host.querySelector(".tm-tip");
  function showTip(region, clientX, clientY) {
    const r = region && rows.get(region);
    if (!r) { tip.hidden = true; return; }
    const rect = host.getBoundingClientRect();
    tip.innerHTML = `<b>${esc(r.name)}</b><span>${esc([r.kind, r.material || "no material", watts(r.power_W)].filter(Boolean).join(" · "))}</span>`;
    tip.title = partDetails(r);
    tip.hidden = false;
    tip.style.left = Math.max(8, Math.min(clientX - rect.left + 14, rect.width - 240)) + "px";
    tip.style.top = Math.min(clientY - rect.top + 14, rect.height - 60) + "px";
  }
  let downXY = null, moveAt = null;
  host.addEventListener("pointerdown", (e) => {
    downXY = [e.clientX, e.clientY];
    if (!(e.target.closest && e.target.closest(".v-tools"))) touched = true;
  }, true);
  host.addEventListener("pointerup", (e) => {
    if (!downXY || (e.target.closest && e.target.closest(".v-tools,.tm-probe"))) return;
    if (Math.hypot(e.clientX - downXY[0], e.clientY - downXY[1]) > 6) return;
    const region = pickAt(e.clientX, e.clientY);
    const next = region && region === selected ? null : region;
    select(next); panel.select(next);
  }, true);
  host.addEventListener("pointermove", (e) => {
    if (e.buttons) { tip.hidden = true; return; }      // turning the model: no hover reading
    const queued = moveAt !== null;
    moveAt = [e.clientX, e.clientY];
    if (queued) return;
    requestAnimationFrame(() => {
      if (!moveAt) return;
      const [x, y] = moveAt; moveAt = null;
      const region = pickAt(x, y);
      if (region !== hovered) { hovered = region; panel.hover(region); paint(); }
      showTip(region, x, y);
    });
  });
  host.addEventListener("pointerleave", () => {
    moveAt = null; tip.hidden = true;
    if (hovered) { hovered = null; panel.hover(null); paint(); }
  });

  /* THE PROBES the engineer will read temperatures at: small pins, named on hover */
  const probes = (t.probes || []).filter((p) => (p.at_mm || []).length === 3).map((p) => {
    const el = document.createElement("div"); el.className = "tm-probe"; el.title = p.name;
    el.setAttribute("aria-label", "probe " + p.name);
    host.appendChild(el);
    return { name: p.name, c: p.at_mm.map((v) => v / 1000), el };
  });
  function probe(name) {
    probes.forEach((p) => p.el.classList.toggle("sel", p.name === name && !p.el.classList.contains("sel")));
  }

  /* THE CAMERA - the three-quarter view framed on the parts, re-fitted as the canvas settles */
  function fit() {
    ren.resetCamera(partBounds);
    const [w, h] = apiRW.getSize(), a = w / Math.max(h, 1);
    if (a < 1) { cam.dolly(a); ren.resetCameraClippingRange(); }
    rw.render(); }
  const upSel = box.querySelector("#gs-up-" + sessionId);
  let upAxis = (upSel && upSel.value) || "+z";
  function iso() {
    cam.setPosition(centre[0] - diag, centre[1] - diag, centre[2] + 0.8 * diag);
    cam.setFocalPoint(...centre); cam.setViewUp(0, 0, 1);
    orient(cam, upAxis, [-1, -1, 0.8]);
    fit();
  }
  const ro = new ResizeObserver(() => { setRenderScale(); if (!touched) fit(); else rw.render(); });
  ro.observe(host);
  const tools = box.querySelector(".v-tools"); shield(tools);
  if (upSel) upSel.onchange = () => { upAxis = upSel.value; saveUp("session", sessionId, upAxis); iso(); };
  const fitBtn = box.querySelector("#gs-fit-" + sessionId); if (fitBtn) fitBtn.onclick = () => { touched = false; iso(); };
  const allBtn = box.querySelector("#gs-all-" + sessionId); if (allBtn) allBtn.onclick = () => { select(null); panel.select(null); };
  const xrayBtn = box.querySelector("#gs-xray-" + sessionId);
  if (xrayBtn) xrayBtn.onclick = () => {
    xray = !xray; xrayBtn.classList.toggle("on", xray); xrayBtn.setAttribute("aria-pressed", xray ? "true" : "false"); paint();
  };

  const axesEl = box.querySelector(".gc-axes");
  const axesLines = ["x", "y", "z"].map((k) => [axesEl.querySelector("line.ax-" + k), axesEl.querySelector("text.ax-" + k)]);
  function drawAxes() {
    const dir = cam.getDirectionOfProjection(), up = cam.getViewUp();
    const right = [dir[1] * up[2] - dir[2] * up[1], dir[2] * up[0] - dir[0] * up[2], dir[0] * up[1] - dir[1] * up[0]];
    const rl = Math.hypot(right[0], right[1], right[2]) || 1;
    right[0] /= rl; right[1] /= rl; right[2] /= rl;
    const upp = [right[1] * dir[2] - right[2] * dir[1], right[2] * dir[0] - right[0] * dir[2], right[0] * dir[1] - right[1] * dir[0]];
    const C = 42, L = 28;
    ["x", "y", "z"].forEach((k, i) => {
      const sx = right[i], sy = upp[i], sz = -dir[i];
      const [line, label] = axesLines[i];
      line.setAttribute("x2", (C + L * sx).toFixed(1)); line.setAttribute("y2", (C - L * sy).toFixed(1));
      label.setAttribute("x", (C + (L + 9) * sx).toFixed(1)); label.setAttribute("y", (C - (L + 9) * sy).toFixed(1));
      line.classList.toggle("back", sz < 0); label.classList.toggle("back", sz < 0);
    });
  }
  let alive = true;
  (function loop() {
    if (!alive || !document.body.contains(host)) return;
    drawAxes();
    const size = apiRW.getSize(), aspect = size[0] / size[1], rect = host.getBoundingClientRect();
    probes.forEach((p) => {
      const nd = ren.worldToNormalizedDisplay(p.c[0], p.c[1], p.c[2], aspect);
      const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
      p.el.style.display = ok ? "" : "none";
      if (ok) { p.el.style.left = (nd[0] * rect.width) + "px"; p.el.style.top = ((1 - nd[1]) * rect.height) + "px"; }
    });
    requestAnimationFrame(loop);
  })();

  setRenderScale(); iso(); paint();

  /* support/debug hook - the browser tier drives the stage through it, without pixel picking.
     Held by identity, as the flow stage's is: a stage replaced by another on the same session must
     not have the live one's hook deleted out from under it when it is stopped. */
  window._vdbg = window._vdbg || {};
  const hook = {
    thermal: true, parts: () => [...parts.keys()], selected: () => selected, lit: () => (lit ? [...lit] : []),
    select: (r) => { select(r); panel.select(r); },
    opacity: (r) => (parts.get(r) ? parts.get(r).actor.getProperty().getOpacity() : null),
    color: (r) => (parts.get(r) ? parts.get(r).actor.getProperty().getColor() : null),
    pickAt,
    screenOf: (pt) => {
      const size = apiRW.getSize(), rect = host.getBoundingClientRect();
      const nd = ren.worldToNormalizedDisplay(pt[0], pt[1], pt[2], size[0] / size[1]);
      return [rect.left + nd[0] * rect.width, rect.top + (1 - nd[1]) * rect.height];
    },
    centreOf: (r) => { const b = parts.get(r).pd.getBounds(); return [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, b[5]]; },
    airbox: () => !!airbox, xray: () => xray, setXray: (on) => { xray = !!on; paint(); },
    probes: () => probes.length, pins: () => 0, outline: () => !!outline,
    iso, frame, cam: () => cam, render: () => rw.render(),
  };
  window._vdbg["gstage:" + sessionId] = hook;
  return {
    select, hover, frame, probe,
    stop: () => {
      alive = false; ro.disconnect();
      if (window._vdbg["gstage:" + sessionId] === hook) delete window._vdbg["gstage:" + sessionId];
      // the same object graph, held the same way: a thermal model's scene is a vtk render window too
      try { apiRW.delete(); } catch { /* already gone */ }
      try { grw.delete(); } catch { /* already gone */ }
    },
  };
}


/** A proposal's value with every length re-read by `f`, by the server's own rule
 *  (application/geometry_check.rescaled_lengths): keys ending _mm or _m by f, areas (_mm2, _m2) by
 *  f squared, at any depth; keys with no unit are left alone. */
function atScale(key, v, f) {
  const by = /_mm2$|_m2$/.test(key) ? f * f : /_mm$|_m$/.test(key) ? f : 0;
  const scale = (x) => (Array.isArray(x) ? x.map(scale) : typeof x === "number" ? x * by : x);
  if (by) return scale(v);
  if (Array.isArray(v)) return v.map((x) => atScale("", x, f));
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, atScale(k, x, f)]));
  return v;
}
