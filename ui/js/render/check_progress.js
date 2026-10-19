// Responsibility: Show the geometry check's live line in the conversation - a small working mark
// with the step running now in plain words, its time counting up, the steps done (ticked, folded
// away), a "still working" note, and a plain message with the way on when a step fails.
// Boundaries: it renders the line the API serves (application/geometry_progress); it fetches
// nothing and decides nothing about the check. The poll that feeds it lives in shell/composer.js.
import { esc } from "../core/format.js";

/** 14 s, 2 min 5 s, 12 min - the server's own words for a duration (duration_words). */
export function durationWords(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60), r = s % 60;
  return r && m < 10 ? `${m} min ${r} s` : `${m} min`;
}

const MARK = { done: "✓", failed: "✕", skipped: "–" };
/* the states a line ends in: nothing on it works any more */
const SETTLED = ["done", "failed", "unsupported"];

/** The steps folded under the line: the ones done (or failed), each with its time. */
function stepsHtml(p) {
  const shown = (p.steps || []).filter((s) => s.state === "done" || s.state === "failed" || s.state === "active");
  return shown.map((s) => {
    // a step that failed is named as what it was doing, not as if it had been done
    const label = s.state === "active" ? s.doing : s.state === "failed" ? String(s.doing || "").replace(/…$/, "") : s.label;
    const t = s.seconds == null || s.state === "active" ? "" : durationWords(s.seconds);
    return `<li class="gp-step ${esc(s.state)}"><span class="gp-tick" aria-hidden="true">${MARK[s.state] || "•"}</span>`
      + `<span class="gp-label">${esc(label)}</span><span class="gp-t num">${esc(t)}</span></li>`;
  }).join("");
}

/** The line's markup for one served progress. Pure, so it is testable without a page.
 *  `opts.open` unfolds the steps; `opts.elapsed` is the running step's time as shown now;
 *  `opts.offline` says the server has not answered for a while. */
export function progressHtml(p, opts = {}) {
  if (!p) return "";
  const state = String(p.state || "");
  const done = (p.steps || []).filter((s) => s.state === "done").length;
  const live = !!p.live && !opts.offline && !opts.ended;
  const time = state === "done" ? "" : (opts.elapsed != null && (state === "running" || state === "queued")
    ? durationWords(opts.elapsed) : "");
  const toggle = done || state === "failed"
    ? `<button type="button" class="gp-toggle" aria-expanded="${opts.open ? "true" : "false"}">`
      + `${state === "done" ? "Details" : `${done} done`}<span class="gp-chev" aria-hidden="true"></span></button>`
    : "";
  let note = p.note || "";
  if (opts.offline) note = "Can't reach the server right now - still trying…";
  if (opts.ended) note = "Stopped checking. Reload the page to see where it got to.";
  const failed = state === "failed" || state === "unsupported";
  const body = failed ? `${p.reason ? `<div class="gp-reason">${esc(p.reason)}</div>` : ""}`
      + `${p.advice ? `<div class="gp-advice">${esc(p.advice)}</div>` : ""}`
      + `${p.can_retry ? '<div class="gp-acts"><button type="button" class="gp-retry">Try again</button></div>' : ""}`
    : "";
  return `<div class="gp" data-state="${esc(state)}"${live ? ' data-live="1"' : ""}${p.slow ? ` data-slow="${esc(p.slow)}"` : ""}>`
    + `<div class="gp-head"><span class="gp-mark" aria-hidden="true">${state === "done" ? "✓" : failed ? "✕" : ""}</span>`
    + `<span class="gp-now" role="status" aria-live="polite">${esc(p.current || "")}</span>`
    + `<span class="gp-time num" aria-hidden="true">${esc(time)}</span>${toggle}</div>`
    + (note ? `<div class="gp-note">${esc(note)}</div>` : "")
    + body
    + `<ol class="gp-steps"${opts.open ? "" : " hidden"}>${stepsHtml(p)}</ol></div>`;
}

/* what a re-draw is for: a change a person can see. The running step's seconds move on every
   poll and are drawn by the ticker, so they do not count - a re-draw on every poll would take the
   focus off a button the user is on. */
function signature(p, opts) {
  if (!p) return "";
  return JSON.stringify([p.state, p.current, p.note, p.slow, p.reason, p.advice, p.can_retry, !!p.live,
    !!opts.offline, !!opts.ended,
    (p.steps || []).map((s) => [s.key, s.state, s.state === "active" ? null : s.seconds])]);
}

/** THE ONE LINE in the conversation. `show` places it on the first progress and follows it after;
 *  while the check works, the line stays at the foot of the conversation, as a "thinking" line
 *  does, and once it is done or failed it stays where it ended. Its time counts up every second
 *  between polls, from the server's own count, so a slow poll never freezes it - and it stops
 *  the moment the check is not working, so nothing spins forever. */
export function createCheckProgress() {
  let el = null, last = null, sig = "", open = false, got = 0, timer = null, flags = {}, retry = null;

  const elapsedNow = () => (last && last.elapsed_s != null ? Number(last.elapsed_s) + (Date.now() - got) / 1000 : null);
  const working = () => !!(last && last.live && !flags.offline && !flags.ended);
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }
  function tick() {
    if (!el || !el.isConnected) { stopTimer(); return; }
    const t = el.querySelector(".gp-time");
    if (t && working()) t.textContent = durationWords(elapsedNow());
  }
  function draw() {
    el.innerHTML = progressHtml(last, { ...flags, open, elapsed: elapsedNow() });
    // folding is done in place: a re-draw would take the focus off the button
    const toggle = el.querySelector(".gp-toggle");
    toggle?.addEventListener("click", () => {
      open = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      const list = el.querySelector(".gp-steps"); if (list) list.hidden = !open;
    });
    const btn = el.querySelector(".gp-retry");
    if (btn) btn.addEventListener("click", () => {
      if (!retry) return;
      btn.disabled = true; btn.textContent = "Starting again…";
      Promise.resolve(retry()).catch(() => { btn.disabled = false; btn.textContent = "Try again"; });
    });
  }

  return {
    /** Follow the served progress. `col` is the conversation column; `opts.retry` runs the
     *  failed step again; `opts.offline` / `opts.ended` mark a line whose server is quiet or whose
     *  watch has stopped. A line the conversation dropped (a new run cleared it) is left dropped. */
    show(col, p, opts = {}) {
      if (opts.retry) retry = opts.retry;
      flags = { offline: !!opts.offline, ended: !!opts.ended };
      const wasSettled = !!(last && SETTLED.includes(last.state));
      if (p) { last = p; got = Date.now(); }
      if (!last || !col) return;
      if (el && !el.isConnected) { stopTimer(); return; }
      let moved = false;
      if (!el) {
        el = document.createElement("div");
        // a PINNED line (the run card's) sits at the top of its host and never moves
        el.className = opts.pinned ? "gp-pinned" : "im assistant gp-row";
        el.id = opts.id || "check-progress";
        if (opts.pinned) col.prepend(el); else col.appendChild(el);
        moved = !opts.pinned;
      }
      // a line still working (or waiting on the answer) stays below the newest message, and ends
      // there; once it has ended, the conversation goes on below it
      if (!opts.pinned && el.nextSibling && !(wasSettled && SETTLED.includes(last.state))) { col.appendChild(el); moved = true; }
      const next = signature(last, flags);
      if (next !== sig) { sig = next; draw(); }
      else tick();
      if (working()) { if (!timer) timer = setInterval(tick, 1000); } else stopTimer();
      // the conversation follows the line only when it moved, never on a poll that changed a word
      const scroller = col.closest(".scroll");
      if (moved && scroller) scroller.scrollTop = scroller.scrollHeight;
    },
    /** What the line holds now: for the ui tier. */
    state() { return { state: last && last.state, live: working(), open, connected: !!(el && el.isConnected) }; },
    reset() { stopTimer(); el?.remove(); el = null; last = null; sig = ""; open = false; flags = {}; retry = null; },
  };
}

export const CheckProgress = createCheckProgress();
