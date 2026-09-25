// Responsibility: Give the Surveyor's receipt a surface of its own, with its three sections told apart by eye.
// Owns: the parse of that block and the DOM it becomes.
// Boundaries: it parses and builds; it fetches nothing, decides nothing about when a panel appears,
//             and appends itself to nothing - the caller mounts what it returns.

/* THE WHOLE POINT OF THIS PANEL IS THE DIFFERENCE BETWEEN ITS THREE SECTIONS.
 *
 *   MEASURED   arithmetic on the customer's own bytes. Simply true. Set as data: solid rule,
 *              monospace, tabular figures, full-contrast text.
 *   SEEN       a vision model's words about rendered pictures. It is a READING, and the plan
 *              checker caps its identity claims at 0.6 confidence for measured reasons. So it
 *              must not LOOK like the measurement: dashed rule, tinted panel, muted prose type,
 *              its own confidence stated in its own word, and never a monospace figure.
 *   SUGGESTS   what the two together imply, offered for correction. Accent rule - the colour
 *              this product reserves for the thing you act on - and the server's own "say X to
 *              change it" offers lifted into buttons.
 *
 * All three were one run of grey text in a chat bubble, which is exactly the distinction this
 * product cannot afford to lose. The difference is carried by RULE STYLE and TYPE as well as by
 * colour, so it survives greyscale and a colour-blind reader.
 *
 * Nothing is dropped. A line whose shape this parser does not recognise is rendered verbatim in
 * the section it arrived in, and a section this build has no name for still renders.
 */
import { esc } from "../core/format.js";

/** The section's rows, with the key column split off where the server wrote one.
 *
 *  `surveyor_panel` writes "  looks like   a solid chamfered block" - a two-space-indented key
 *  followed by at least two spaces and its value - and plain indented sentences with no key at
 *  all ("  1,044 x 389 x 288 mm"). Both are kept; a line that is neither is kept as its own
 *  value, because a line the parser cannot read is still a line the customer was sent. */
export function parsePanel(block) {
  const sections = [];
  let current = null;
  for (const raw of String(block || "").split("\n")) {
    if (!raw.trim()) continue;
    const head = raw === raw.trimStart()
      ? raw.trim().match(/^([A-Z][A-Z0-9 ]{2,}[A-Z0-9])\s*(?:\((.*)\))?$/)
      : null;
    if (head) {
      current = { name: head[1], note: head[2] || "", rows: [] };
      sections.push(current);
      continue;
    }
    if (!current) {
      current = { name: "", note: "", rows: [] };
      sections.push(current);
    }
    const body = raw.trim();
    const pair = body.match(/^(\S.*?)\s{2,}(\S.*)$/);
    current.rows.push(pair ? { key: pair[1], value: pair[2] } : { key: "", value: body });
  }
  return sections;
}

/** Consecutive rows under one key are one row with several values - the look reports three
 *  separate "sharp" findings and they are one observation, not three sections. */
function groupRows(rows) {
  const out = [];
  for (const row of rows) {
    const last = out[out.length - 1];
    if (row.key && last && last.key === row.key) last.values.push(row.value);
    else out.push({ key: row.key, values: [row.value] });
  }
  return out;
}

/** The words the SERVER offered as a correction - "say draft or max to change it". Lifted into
 *  buttons verbatim and never invented: a chip this UI made up would be a promise the
 *  conversation has not agreed to honour. */
function offeredWords(text) {
  const say = String(text || "").match(/\bsay ([^-.]+?) to change it/i);
  if (!say) return [];
  return say[1].split(/\s*(?:,|\bor\b)\s*/)
    .map((w) => w.trim().replace(/^["'`]|["'`]$/g, ""))
    .filter((w) => w && w.length < 24 && /^[\w .+-]+$/.test(w));
}

/** How many views were read, and how long it took, from the SEEN heading's own note. Absent
 *  when the note does not say - this never estimates either one. */
function readingStats(note) {
  const views = String(note || "").match(/(\d+)\s+rendered views/);
  const seconds = String(note || "").match(/(\d+(?:\.\d+)?)s(?:\b|\))/);
  return { views: views ? parseInt(views[1], 10) : 0, seconds: seconds ? seconds[1] : "" };
}

/** Three named strengths and nothing else. A word this build has never seen is SHOWN as the word
 *  with no meter beside it, rather than being rounded to the nearest bar it never claimed. */
const CONFIDENCE_STEPS = { low: 1, medium: 2, high: 3 };

function confidenceHtml(word) {
  const level = CONFIDENCE_STEPS[String(word || "").trim().toLowerCase()] || 0;
  const bars = level
    ? [1, 2, 3].map((n) => `<i class="${n <= level ? "on" : ""}"></i>`).join("")
    : "";
  return `<span class="sv-conf" title="the reading's own confidence in what it says">`
    + `<span class="sv-conf-l">its own confidence</span>`
    + (bars ? `<span class="sv-conf-m">${bars}</span>` : "")
    + `<b>${esc(word)}</b></span>`;
}

/** A filmstrip of N frames. Deliberately ABSTRACT marks, not thumbnails: the rendered views are
 *  not in anything the browser is sent, and seventeen grey rectangles pretending to be pictures
 *  of the customer's part would be the exact lie this product refuses elsewhere. The marks say
 *  how many views were read; the words beside them are what came back. */
function filmstripHtml(views) {
  if (!views || views > 64) return "";
  return `<span class="sv-strip" aria-hidden="true">`
    + new Array(views).fill('<i></i>').join("") + `</span>`;
}

function rowsHtml(rows) {
  return groupRows(rows).map((row) => {
    const values = row.values.map((v) => `<span class="sv-v">${esc(v)}</span>`).join("");
    if (!row.key) return `<div class="sv-row sv-row-line">${values}</div>`;
    return `<div class="sv-row"><span class="sv-k">${esc(row.key)}</span>`
      + `<span class="sv-vs">${values}</span></div>`;
  }).join("");
}

/** Which of the three this section is. Unknown names still render, under their own name and with
 *  the neutral treatment - a section added on the server must not vanish from the browser. */
function kindOf(name) {
  const n = String(name || "").toUpperCase();
  if (n.startsWith("MEASURED")) return "measured";
  if (n.startsWith("SEEN")) return "seen";
  if (n.startsWith("SUGGESTS")) return "suggests";
  return "other";
}

const CAPTION = {
  // Said by the UI, about what the section IS - not a restatement of any value in it.
  seen: "A vision model was shown rendered pictures of your part and described them. "
      + "It is a reading, not a measurement. Correct anything it got wrong.",
};

function sectionHtml(section) {
  const kind = kindOf(section.name);
  const rows = section.rows.slice();
  let extra = "";

  if (kind === "seen") {
    const { views, seconds } = readingStats(section.note);
    const confidence = rows.findIndex((r) => /confidence/i.test(r.key));
    let meter = "";
    if (confidence >= 0) {
      meter = confidenceHtml(rows[confidence].value);
      rows.splice(confidence, 1);          // shown in the heading instead, same word
    }
    const strip = filmstripHtml(views);
    const counted = [views ? `${views} views read` : "", seconds ? `${seconds}s` : ""]
      .filter(Boolean).join(" · ");
    extra = `<div class="sv-reading">${strip}`
      + (counted ? `<span class="sv-count num">${esc(counted)}</span>` : "")
      + meter + `</div>`;
  }

  const offers = kind === "suggests"
    ? [...new Set(rows.flatMap((r) => offeredWords(r.value)))]
    : [];
  const chips = offers.length
    ? `<div class="sv-offers"><span class="sv-offers-l">reply with</span>`
      + offers.map((w) =>
        `<button type="button" class="sv-chip" data-say="${esc(w)}">${esc(w)}</button>`).join("")
      + `</div>`
    : "";

  return `<section class="sv-sec sv-${kind}">`
    + `<div class="sv-sec-h"><span class="sv-tag">${esc(section.name || "Also")}</span>`
    + (section.note ? `<span class="sv-note">${esc(section.note)}</span>` : "")
    + `</div>`
    + extra
    + `<div class="sv-rows">${rowsHtml(rows)}</div>`
    + (CAPTION[kind] ? `<p class="sv-cap">${esc(CAPTION[kind])}</p>` : "")
    + chips
    + `</section>`;
}

/** The one line in the collapsed header. Built only from values the panel actually carries: the
 *  first measured figure, how many openings, how long the look took. Anything absent is left out
 *  rather than filled in. */
function summaryOf(sections) {
  const bits = [];
  const measured = sections.find((s) => kindOf(s.name) === "measured");
  if (measured) measured.rows.slice(0, 2).forEach((r) => bits.push(r.value));
  const seen = sections.find((s) => kindOf(s.name) === "seen");
  if (seen) {
    const looks = seen.rows.find((r) => /looks like/i.test(r.key));
    if (looks) bits.push(looks.value);
  }
  return bits.join("  ·  ");
}

/** The panel, as an element the caller mounts. `onSay` receives a word the customer can reply
 *  with, exactly as the server offered it. */
export function buildSurveyor(block, { onSay } = {}) {
  const sections = parsePanel(block);
  const wrap = document.createElement("div");
  wrap.className = "sv open";

  if (!sections.length) {
    // Unparseable. Show the block as it arrived rather than showing nothing: a receipt the
    // browser could not read is still a receipt the customer is owed.
    wrap.innerHTML = `<div class="sv-head"><span class="sv-eyebrow">The Surveyor</span></div>`
      + `<div class="sv-body"><pre class="sv-raw">${esc(block)}</pre></div>`;
    return wrap;
  }

  wrap.innerHTML = `<button type="button" class="sv-head" aria-expanded="true">
      <span class="sv-eyebrow">The Surveyor</span>
      <span class="sv-sum">${esc(summaryOf(sections))}</span>
      <span class="sv-chev" aria-hidden="true">∨</span>
    </button>
    <div class="sv-body">${sections.map(sectionHtml).join("")}</div>`;

  const head = wrap.querySelector(".sv-head");
  head.onclick = () => {
    const open = wrap.classList.toggle("open");
    head.setAttribute("aria-expanded", String(open));
  };
  wrap.querySelectorAll(".sv-chip").forEach((chip) => {
    chip.onclick = () => onSay && onSay(chip.dataset.say);
  });
  return wrap;
}
