// Responsibility: Say that Hexera is working, and then say what those seconds were spent on.
// Owns: the live strip, this session's own reply timings, and the receipt built from the turn's trace.
// Boundaries: it renders; it opens no socket and calls no endpoint. The trace is handed to it.

/* TWO HALVES OF THE SAME PROMISE: "tell me it is running, then show me what it did".
 *
 * WHILE THE TURN IS IN FLIGHT the browser knows exactly one honest thing - how long it has been
 * waiting. There is no live channel during intake: /api/v1/ws/ticket mints a ticket only for a
 * row in simulation_jobs, and during the conversation no job exists yet (asked for, and it
 * answers 404 "Job not found or access denied"). So this strip claims nothing about WHAT is
 * happening. It counts, and it bars that count against the median of the replies THIS SESSION
 * has already measured - the same rule the mesh bar follows: a real reference or none, never an
 * invented percentage, and past the reference it holds full and keeps counting rather than
 * pretending the wait is over.
 *
 * WHEN THE TURN LANDS the reply carries `trace` - the turn's public reasoning rounds, tool
 * lifecycle and application rationale, already projected for the deployment's trace mode. The
 * browser has been throwing it away since it was added. It is now the receipt: what ran, in what
 * order, and how long each part took. The 27 seconds stop being a hang and become "it surveyed
 * your part: 20.1s of that was the look".
 *
 * It runs every event through the SAME dispatch the live timeline uses, with a renderer of its
 * own, so an event type only ever has to be taught to the product once.
 */
import { dispatch, reasoningHeader } from "../core/events.js";
import { esc } from "../core/format.js";

/** Reply durations this page has MEASURED, in milliseconds. Not persisted and not shared between
 *  sessions: a median built from somebody else's part on somebody else's network is not a
 *  statement about this wait. */
let measured = [];

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Seconds to one decimal - the unit every duration in the trace arrives in. `fmtDur` rounds to
 *  whole seconds, which turns a 58 ms tool call into "0:00" and loses the point of showing it. */
function secs(ms) {
  return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + "s";
}

const ORB = `<svg class="act-orb" viewBox="0 0 24 24" aria-hidden="true">
  <circle class="sp-arm" cx="12" cy="12" r="7.2"/>
  <path class="sp-hd" d="M19.20 8.16L22.40 14.56L16.00 14.56Z"/>
  <path class="sp-hd" d="M4.80 15.84L1.60 9.44L8.00 9.44Z"/></svg>`;

/* THE RECEIPT'S RENDERER.
 *
 * Every method the event dispatch can call, writing into one flat list. A pre-job turn is one
 * stage by construction - the trace sink stamps every event "intake" - so there are no lanes to
 * keep, and the methods that only mean something inside a mesh run are accepted and ignored
 * rather than missing: an unknown call must not throw away the rest of the receipt.
 */
function receiptRenderer(body) {
  const seen = { think: {}, tools: {} };
  const add = (html) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = html;
    body.appendChild(row);
    return row;
  };
  return {
    counted: 0,
    node() {}, done() {}, attempt() {}, startMesh() {}, endMesh() {}, screenshot() {},
    check(_stage, text, ok) {
      this.counted++;
      add(`<div class="e-check${ok ? "" : " bad"}"><span class="ic">${ok ? "✓" : "✕"}</span>`
        + `<span class="ct">${esc(text)}</span></div>`);
    },
    info(_stage, text, warn) {
      this.counted++;
      add(`<div class="e-info${warn ? " warn" : ""}">${esc(text)}</div>`);
    },
    tool(_stage, action) {
      this.counted++;
      add(`<div class="e-tool"><span class="tag">step</span>`
        + `<span class="call">${esc(action)}</span></div>`);
    },
    mcp(_stage, query) {
      this.counted++;
      add(`<div class="e-tool"><span class="tag">search</span>`
        + `<span class="call">${esc(query || "")}</span></div>`);
    },
    file(_stage, path, bytes, op) {
      this.counted++;
      add(`<div class="e-tool"><span class="tag">${esc(op || "written")}</span>`
        + `<span class="call">${esc(path)}</span><span class="tres"> · ${esc(bytes || "")}</span></div>`);
    },
    reasoning(_stage, ev) {
      // One card per reasoning id across its lifecycle, exactly as the live timeline does it:
      // "started" and "completed" are the same round, and rendering them as two rows would
      // double every count on the receipt.
      const id = ev.id || ("anon-" + this.counted);
      let card = seen.think[id];
      if (!card) {
        card = add('<div class="e-think"><div class="lbl"></div><div class="body"></div></div>');
        seen.think[id] = card;
        this.counted++;
      }
      const head = reasoningHeader(ev);
      const label = card.querySelector(".lbl");
      label.textContent = head.text;
      label.classList.toggle("measured", head.measured);
      const text = card.querySelector(".body");
      if (ev.content) { text.textContent = ev.content; text.style.display = ""; }
      else { text.textContent = ""; text.style.display = "none"; }
    },
    toolCall(_stage, ev) {
      if (ev.id && seen.tools[ev.id]) return;
      this.counted++;
      // The PUBLIC LABEL leads, because it is the sentence written for a customer; the real tool
      // name follows only when this deployment's trace mode sent one. The browser never maps one
      // to the other - a browser that could has already been told the name.
      const blocked = ev.status === "blocked";
      const row = add(`<div class="e-tool${blocked ? " bad" : ""}">`
        + `<span class="tag">${blocked ? "blocked" : "step"}</span>`
        + `<span class="call">${esc(ev.public_label || ev.tool_name || "")}</span>`
        + (ev.public_label && ev.tool_name
          ? `<span class="tres"> · ${esc(ev.tool_name)}</span>` : "")
        + `</div>`);
      if (ev.id) seen.tools[ev.id] = row;
    },
    toolResult(_stage, ev) {
      const row = ev.tool_call_id && seen.tools[ev.tool_call_id];
      const bits = [];
      if (ev.status && ev.status !== "success") bits.push(ev.status);
      if (typeof ev.duration_ms === "number") bits.push(secs(ev.duration_ms));
      if (row && !row.dataset.done) {
        row.dataset.done = "1";
        if (bits.length) {
          const mark = document.createElement("span");
          mark.className = "tres";
          mark.textContent = " · " + bits.join(" · ");
          row.querySelector(".e-tool").appendChild(mark);
        }
        return;
      }
      this.counted++;
      add(`<div class="e-tool${ev.status === "success" ? "" : " bad"}">`
        + `<span class="tag">result</span>`
        + `<span class="call">${esc(ev.public_label || ev.tool_name || "")}</span>`
        + (bits.length ? `<span class="tres"> · ${esc(bits.join(" · "))}</span>` : "") + `</div>`);
    },
    rationale(_stage, ev) {
      this.counted++;
      add(`<div class="e-rat"><div class="rat-c">${esc(ev.conclusion || "")}</div>`
        + (ev.because ? `<div class="rat-w">${esc(ev.because)}</div>` : "") + `</div>`);
    },
  };
}

export const Activity = {
  _el: null, _t0: 0, _tick: null, _ref: 0, _done: "Replied in", _counts: true,

  /** A new run on this page starts with no timing history: the previous run's numbers describe a
   *  different part and a different conversation. */
  reset() {
    this.stop();
    measured = [];
  },

  /** Mount the live strip.
   *
   *  `label` says what the BROWSER is doing - sending a message, uploading a file - and never
   *  what the server is doing with it. `done` is the verb the finished line uses, because
   *  "Replied in 3.2s" is the wrong sentence for an upload. `counts` is whether this wait joins
   *  the median: an upload and a conversational turn are different operations over different
   *  amounts of data, and averaging them together would produce a "usually" that describes
   *  neither. */
  start(mount, { label = "", done = "Replied in", counts = true } = {}) {
    this.stop();
    const el = document.createElement("div");
    el.className = "act live";
    const typical = counts ? median(measured) : 0;
    this._ref = typical;
    this._done = done;
    this._counts = counts;
    // What the sub-line may say, in order of how much is actually known:
    //   a measured median  -> bar the clock against it and say what it is built from
    //   replies, none yet  -> say plainly that there is nothing to compare this one with
    //   not a reply at all -> say nothing. An upload has no reply history and inventing one
    //                         from the conversation's timings would describe a different job.
    const sub = typical
      ? `usually ${secs(typical)} here, measured over ${measured.length} repl`
        + `${measured.length === 1 ? "y" : "ies"} in this session`
      : counts ? "first reply of this session - nothing measured to compare it with yet" : "";
    el.innerHTML = `<div class="act-top">${ORB}`
      + `<span class="act-lbl">${esc(label || "Hexera is working")}</span>`
      + `<span class="act-clock num">0.0s</span></div>`
      + (typical ? `<div class="act-track"><div class="act-fill"></div></div>` : "")
      + (sub ? `<div class="act-sub">${esc(sub)}</div>` : "");
    this._el = mount(el);
    this._t0 = Date.now();
    this._tick = setInterval(() => this.tick(), 100);
    this.tick();
    return el;
  },

  tick() {
    if (!this._el) return;
    const elapsed = Date.now() - this._t0;
    const clock = this._el.querySelector(".act-clock");
    if (clock) clock.textContent = secs(elapsed);
    const fill = this._el.querySelector(".act-fill");
    if (fill && this._ref) {
      fill.style.width = Math.min(100, (elapsed / this._ref) * 100) + "%";
      if (elapsed >= this._ref) {
        fill.classList.add("over");
        const sub = this._el.querySelector(".act-sub");
        if (sub && !sub.dataset.over) {
          sub.dataset.over = "1";
          sub.textContent = `longer than usual - ${secs(this._ref)} is typical here. `
            + "The run is still going; nothing has failed.";
        }
      }
    }
  },

  stop() {
    if (this._tick) { clearInterval(this._tick); this._tick = null; }
    this._el = null;
  },

  /** The turn landed. The strip becomes the receipt, in place, so the thing that was counting
   *  is the thing that now says what it was counting. */
  finish(trace) {
    const el = this._el;
    const elapsed = this._t0 ? Date.now() - this._t0 : 0;
    this.stop();
    if (!el) return;
    if (elapsed && this._counts) measured.push(elapsed);

    const events = Array.isArray(trace) ? trace : [];
    if (!events.length) {
      // No trace for this turn - a turn the authority settled with no model call, or a
      // deployment whose trace mode publishes nothing. Say how long it took and stop there
      // rather than offering an empty "what it did" a reader would open and find blank.
      el.className = "act done bare";
      el.innerHTML = `<span class="act-tick" aria-hidden="true">✓</span>`
        + `<span class="act-lbl">${esc(this._done)} ${esc(secs(elapsed))}</span>`;
      return;
    }

    el.className = "act done";
    el.innerHTML = `<button type="button" class="act-head" aria-expanded="false">
        <span class="act-tick" aria-hidden="true">✓</span>
        <span class="act-lbl">Worked for ${esc(secs(elapsed))}</span>
        <span class="act-ct"></span>
        <span class="act-chev" aria-hidden="true">∨</span>
      </button><div class="act-body"></div>`;
    const body = el.querySelector(".act-body");
    const renderer = receiptRenderer(body);
    let cursor = null;
    for (const ev of events) cursor = dispatch(ev, renderer, cursor);
    el.querySelector(".act-ct").textContent =
      renderer.counted + (renderer.counted === 1 ? " step" : " steps");
    const head = el.querySelector(".act-head");
    head.onclick = () => {
      const open = el.classList.toggle("open");
      head.setAttribute("aria-expanded", String(open));
    };
  },

  /** The turn failed. The strip must not be left spinning forever on a page whose request is
   *  already dead - a spinner that never stops reads as a hang in the product, not in the
   *  network. */
  fail() {
    const el = this._el;
    const elapsed = this._t0 ? Date.now() - this._t0 : 0;
    this.stop();
    if (!el) return;
    el.className = "act done bare fail";
    el.innerHTML = `<span class="act-tick" aria-hidden="true">✕</span>`
      + `<span class="act-lbl">Stopped after ${esc(secs(elapsed))}</span>`;
  },
};
