// Responsibility: The one viewer of the intake - the user's part beside the conversation from the moment it is
// drawn until the mesher starts: the workbench takeover, the split between the chat and the model (a divider the
// user drags, remembered), and the parts mode the notes' rows are drawn in on the same viewer.
// Owns: the held intake viewer, the divider and its stored width, the host the parts model is mounted in.
// Boundaries: it draws nothing in 3D itself - the geometry stage and the parts view do - and it decides nothing
// about the check or the notes. It only says where they are drawn and when the viewer goes.

/* THE WORKBENCH TAKEOVER, the same move the mesh viewer makes: with a workbench on the page the box fills it and
   the conversation becomes the drawer; without one it opens inline. Releasing gives the page back exactly as it
   was unless another viewer is still waiting in the workbench. */
export function takeOver(box, anchorEl) {
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

/* THE SPLIT: the conversation on the left (a third by default), the model on the right; the line between them
   is dragged to give either more room, and the width is kept for next time (as a share of the window). */
const KEY = "hexera.intake.chat-share";
const DEFAULT_SHARE = 1 / 3;
export const CHAT_MIN = 320, VIEW_MIN = 420;
let share = DEFAULT_SHARE, divider = null, onResize = null;

function readShare() {
  try { const v = Number(localStorage.getItem(KEY)); if (v > 0.05 && v < 0.95) return v; } catch { /* private mode */ }
  return DEFAULT_SHARE;
}
function keepShare(v) { try { localStorage.setItem(KEY, String(v)); } catch { /* private mode: not kept */ } }

/** The conversation's width in px for a share of the page, held between the two minimums. */
export function chatWidth(total, s) {
  const max = Math.max(CHAT_MIN, total - VIEW_MIN);
  return Math.round(Math.min(Math.max(s * total, CHAT_MIN), max));
}
function apply() {
  const app = document.getElementById("app");
  if (!app) return;
  const total = app.clientWidth || window.innerWidth;
  const w = chatWidth(total, share);
  app.style.setProperty("--intake-w", w + "px");
  if (divider) { divider.setAttribute("aria-valuenow", String(Math.round(100 * w / total))); }
}

function splitOn() {
  const app = document.getElementById("app"), wb = document.getElementById("workbench");
  if (!app || !wb) return;
  share = readShare();
  app.classList.add("intake");
  apply();
  if (!divider) {
    divider = document.createElement("div");
    divider.className = "iv-split";
    divider.setAttribute("role", "separator");
    divider.setAttribute("aria-orientation", "vertical");
    divider.setAttribute("aria-label", "Drag to resize the conversation and the model");
    divider.setAttribute("aria-valuemin", "0"); divider.setAttribute("aria-valuemax", "100");
    divider.tabIndex = 0;
    let drag = null;
    divider.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      drag = { x: e.clientX, w: parseFloat(getComputedStyle(app).getPropertyValue("--intake-w")) || chatWidth(app.clientWidth, share) };
      divider.setPointerCapture(e.pointerId);
      app.classList.add("iv-dragging");
    });
    divider.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const total = app.clientWidth;
      share = chatWidth(total, (drag.w + e.clientX - drag.x) / total) / total;
      apply();
    });
    const end = (e) => {
      if (!drag) return;
      drag = null;
      try { divider.releasePointerCapture(e.pointerId); } catch { /* already released */ }
      app.classList.remove("iv-dragging");
      keepShare(share);
    };
    divider.addEventListener("pointerup", end);
    divider.addEventListener("pointercancel", end);
    // the keyboard moves it too: the arrows a step, Home back to a third
    divider.addEventListener("keydown", (e) => {
      const total = app.clientWidth, step = 24 / total;
      if (e.key === "ArrowLeft") share -= step; else if (e.key === "ArrowRight") share += step;
      else if (e.key === "Home") share = DEFAULT_SHARE; else return;
      e.preventDefault();
      share = chatWidth(total, share) / total;
      apply(); keepShare(share);
    });
    // a double-click puts it back at a third
    divider.addEventListener("dblclick", () => { share = DEFAULT_SHARE; apply(); keepShare(share); });
  }
  if (divider.parentNode !== wb) wb.prepend(divider);
  if (!onResize) { onResize = () => apply(); window.addEventListener("resize", onResize); }
}

function splitOff() {
  const app = document.getElementById("app");
  if (app) { app.classList.remove("intake", "iv-dragging"); app.style.removeProperty("--intake-w"); }
  if (divider) divider.remove();
  if (onResize) { window.removeEventListener("resize", onResize); onResize = null; }
}

/* THE HELD VIEWER: the geometry stage's box (or, when the stage never opened, a box of the parts model's own),
   kept from the moment the part is drawn until the run starts. */
let held = null;      // {box, release()}
let partsEl = null;   // the parts model's host inside the held box, while the notes' rows are shown

/** The geometry stage opened as the intake's viewer: `release` gives the workbench back and stops its scene. */
export function holdIntake(box, release) {
  if (held && held.box !== box) endIntake();
  held = { box, release };
  splitOn();
}

/** The held viewer, or null. */
export function intakeBox() { return held && held.box.isConnected ? held.box : null; }

/** The check answered: the stage's panel goes, the part stays where it is, alone in the viewer. */
export function settleIntake(box) {
  if (!box) return;
  const panel = box.querySelector(".v-bar");
  if (panel) panel.hidden = true;
  box.classList.add("iv-plain");
}

/** Where the parts model is drawn: a host laid over the held viewer's model (the same place on the page), or a
 *  viewer of its own when no stage is open. Null with no workbench on the page (the caller draws in the card). */
export function partsHost() {
  if (!document.getElementById("workbench")) return null;
  if (partsEl && partsEl.isConnected) return partsEl;
  let box = intakeBox();
  if (!box) {
    box = document.createElement("div");
    box.className = "viewer iv-view iv-plain";
    box.innerHTML = '<div class="v-canvas"></div>';
    const release = takeOver(box, null);
    holdIntake(box, release);
    box.dataset.own = "1";
  }
  // BESIDE the stage's canvas, laid over it in the same cell - never inside it: the stage's own mouse would take
  // every press on the parts model (a drag turned both, a click never reached the parts)
  partsEl = document.createElement("div");
  partsEl.className = "iv-parts";
  box.appendChild(partsEl);
  box.classList.add("iv-parts-on");
  return partsEl;
}

/** The notes' rows are gone (or the model could not be drawn): the viewer shows the part as it was. */
export function dropPartsHost(el) {
  const box = el && el.closest(".viewer");
  if (el) el.remove();
  if (el === partsEl) partsEl = null;
  if (!box) return;
  box.classList.remove("iv-parts-on");
  // a viewer that was only ever the parts model has nothing left to show
  if (box.dataset.own === "1" && held && held.box === box) endIntake();
}

/** The run started (or the page moved on): the viewer goes, and the conversation has the page back. */
export function endIntake() {
  const h = held;
  held = null; partsEl = null;
  if (h) { try { h.release(); } catch (e) { console.warn("intake viewer: release failed", e); } }
  splitOff();
}
