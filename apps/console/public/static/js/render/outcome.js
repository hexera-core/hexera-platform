// Responsibility: Turn the backend's closing message for a finished run into a summary a person can read.
// Boundaries: pure text in, HTML out - no DOM, no network. Every word of the message is kept: what is
// not on the short summary sits behind a "Details" toggle, never dropped.

/* THE CLOSING MESSAGE IS ONE STRING (application/final_result.py render_message): an account of
 * the outcome, then the lines the backend adds. Its shape is regular, and that is all this reads:
 *
 *   Delivered with the reviewer's concerns:          a heading for the rows under it
 *     - IMPORTANT - Cells degrade where ...          a row (two-space indent, a dash)
 *       Reviewer: The directly shown wall ...        the row's own detail (deeper indent)
 *   Every automatic check on the mesh passed - ...   a paragraph
 *   Mesh generation completed successfully.          the outcome itself
 *   Engine: cfmesh                                   a short "label: value" fact
 *
 * Shown as a chat line it was a wall of text: indented dashes, a paragraph per line, a 400-word
 * reviewer finding in the middle. Here it becomes a headline, the rows as a list (each row's
 * detail folded under it), the facts as a small table and long paragraphs cut to their first
 * sentence with the whole text under one "Details". A message that does not have this shape - a
 * one-line ending - comes out as that one line, exactly as it was said.
 */
import { esc } from "../core/format.js";
import { prettyText } from "../core/engineering_text.js";

const SUCCESS = /^Mesh generation completed successfully\.?$/;
const FAILURE = /^Mesh generation did not complete successfully\.?$/;
const BULLET = /^[-*•]\s+(.*)$/;
const FACT = /^([A-Z][A-Za-z]*(?: [A-Za-z]+){0,3}): (\S.*)$/;
const IMPORTANT = /^IMPORTANT\s*[-–:]\s*/;
const NOTE_PREFIX = /^Reviewer note:\s*/;
/* a paragraph or a row longer than this is cut to its first sentence on the summary */
const SHORT = 170;

/** Plain words for the backend's fact labels; anything else keeps its own label. */
const FACT_LABELS = {
  "Mesh detail preference": "Detail level",
  "Required deliverable": "Download",
};

/** The message as blocks: {kind, blocks: [{type:"list", title, items:[{text, important, more}]}
 *  | {type:"para", text}], facts: [[label, value]]}. Pure; nothing is dropped. */
export function parseOutcome(text) {
  const out = { kind: "", blocks: [], facts: [] };
  let list = null, item = null;
  for (const raw of String(text == null ? "" : text).replace(/\r/g, "").split("\n")) {
    const t = raw.trim();
    if (!t) { list = null; item = null; continue; }
    const indent = raw.length - raw.trimStart().length;
    const bullet = t.match(BULLET);
    if (SUCCESS.test(t)) { out.kind = out.kind || "done"; list = null; item = null; continue; }
    if (FAILURE.test(t)) { out.kind = out.kind || "failed"; list = null; item = null; continue; }
    // A ROW'S OWN DETAIL: anything indented deeper than the row it follows
    if (item && indent >= 4) { item.more.push(bullet ? bullet[1] : t); continue; }
    if (bullet) {
      if (!list) { list = { type: "list", title: "", items: [] }; out.blocks.push(list); }
      const words = bullet[1].replace(NOTE_PREFIX, "");
      item = { text: words.replace(IMPORTANT, ""), important: IMPORTANT.test(words), more: [] };
      list.items.push(item);
      continue;
    }
    item = null;
    if (t.endsWith(":") && t.length <= 160) {
      list = { type: "list", title: t.slice(0, -1), items: [] };
      out.blocks.push(list);
      continue;
    }
    list = null;
    // a FACT is short: "Engine: cfmesh", "Attempts used: 2/3." - never a sentence that has a colon
    const fact = t.length <= 90 ? t.match(FACT) : null;
    if (fact && (t.length <= 40 || !/[.!?]$/.test(fact[2].replace(/\)$/, "")))) {
      out.facts.push([FACT_LABELS[fact[1]] || fact[1], fact[2].replace(/\.$/, "")]);
      continue;
    }
    out.blocks.push({ type: "para", text: t });
  }
  return out;
}

/** A long decimal on the summary reads as an engineer would print it: 71.0953 -> 71.1,
 *  3.14159 -> 3.14, 0.000412 -> 0.000412 (three significant figures). The full number stays in
 *  the details. Only a plain decimal with three or more places is touched - never a version, a
 *  date or a run of digits. */
export function roundNumbers(s) {
  return String(s).replace(/(^|[^\d.])(\d+)\.(\d{3,})(?![\d.])/g, (m, pre, whole, frac) => {
    const v = Number(`${whole}.${frac}`);
    if (!Number.isFinite(v)) return m;
    const a = Math.abs(v);
    const r = a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : String(Number(v.toPrecision(3)));
    return pre + r;
  });
}

/** The first sentence of a long text, or the text itself when it is short. */
export function lead(text) {
  const t = String(text);
  if (t.length <= SHORT) return t;
  const m = t.match(/^(.{20,}?[.!?])\s+(?=[A-Z("'])/);
  if (m && m[1].length <= SHORT + 60) return m[1];
  const cut = t.slice(0, SHORT);
  return cut.slice(0, Math.max(cut.lastIndexOf(" "), 40)).replace(/[\s,;:-]+$/, "") + "…";
}

/* escaped first, then the two marks the chat has always honoured: **bold** and `code` */
const fmt = (s) => esc(prettyText(String(s)))
  .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`(.+?)`/g, "<code>$1</code>");
const fmtShort = (s) => fmt(roundNumbers(s));
/* "Review: delivered with concerns (see above)" - the pointer is the summary itself now */
const factValue = (v) => String(v).replace(/\s*\(see above\)$/, "");

function detailsHtml(parts, label = "Details") {
  const body = parts.filter(Boolean).map((p) => `<p>${fmt(p)}</p>`).join("");
  return body ? `<details class="oc-more"><summary>${esc(label)}</summary>${body}</details>` : "";
}

const HEADLINE = { done: "Your mesh is ready.", failed: "No mesh this time." };

/** The closing message as HTML. `opts.concerns` marks a delivered mesh with points to weigh. */
export function outcomeHtml(text, opts = {}) {
  const o = parseOutcome(text);
  const later = [];          // the full text of every paragraph the summary cut short
  let html = "";
  const head = o.kind === "done" && opts.concerns ? "Your mesh is ready, with points to weigh."
    : o.kind === "done" && opts.notes ? "Your mesh is ready, with notes."
    : HEADLINE[o.kind] || "";
  if (head) html += `<p class="oc-head">${esc(head)}</p>`;
  // A FAILED RUN'S PARAGRAPHS ARE ITS POINT - what went wrong and the way forward - so they are
  // never cut short; a delivered mesh's are the standing wording around the facts, cut to a sentence
  const whole = o.kind === "failed";
  for (const b of o.blocks) {
    if (b.type === "para") {
      const short = whole ? b.text : lead(b.text);
      html += `<p class="oc-p">${fmtShort(short)}</p>`;
      if (short !== b.text) later.push(b.text);
      continue;
    }
    // "Reviewer notes (an AI reviewer looked at the mesh; ...):" - the aside goes under the heading
    const aside = b.title.match(/^(.*?\S)\s*\((.{20,})\)$/);
    if (aside) html += `<p class="oc-h">${fmt(aside[1])}</p>`
      + `<p class="oc-sub">${fmt(aside[2].charAt(0).toUpperCase() + aside[2].slice(1))}.</p>`;
    else if (b.title) html += `<p class="oc-h">${fmt(b.title)}</p>`;
    if (!b.items.length) continue;
    html += `<ul class="oc-list">${b.items.map((it) => {
      const short = lead(it.text);
      const more = [short !== it.text ? it.text : "", ...it.more];
      return `<li class="oc-row${it.important ? " important" : ""}">`
        + (it.important ? '<span class="oc-tag">Important</span>' : "")
        + `<span class="oc-txt">${fmtShort(short)}</span>${detailsHtml(more)}</li>`;
    }).join("")}</ul>`;
  }
  if (o.facts.length) {
    html += `<dl class="oc-facts">${o.facts.map(([k, v]) =>
      `<div class="oc-fact"><dt>${fmt(k)}</dt><dd>${fmtShort(factValue(v))}</dd></div>`).join("")}</dl>`;
  }
  html += detailsHtml(later);
  return `<div class="oc${o.kind ? " oc-" + o.kind : ""}">${html}</div>`;
}
