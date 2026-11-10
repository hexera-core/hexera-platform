// Responsibility: The short result screen beside a delivered mesh (revamp 5/5): a one-line verdict, three or four key numbers, the downloads (Everything first, each file, then the Mesh report) and two folded lists - Notes and Hot spots - each grouped by kind.
// Boundaries: pure text in, HTML out, plus one binder for the hot-spot rows and the report download. Everything long - every limit, every place, every hot spot, the per-solver checks, the pairs - lives in the Mesh report (render/mesh_report.py), never in a list here.
// Collaborates with: viewer/viewer.js (mounts it), viewer/fea_results.js (the FEA block's short lines), api/v1/simulation.py (GET /report.html), render/download.js (fresh links).
import { esc, fmtBytes } from "../core/format.js";
import { downloadAttrs } from "../render/download.js";
import { roundNumbers } from "../render/outcome.js";

/* an attribute value: esc() leaves quotes alone */
const attr = (s) => esc(s).replace(/"/g, "&quot;");

const ICON = {
  pass: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>',
  // a pass with notes is still a pass: the same tick, in a quieter tone (no warning mark)
  notes: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>',
  fail: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3.2 3.2l5.6 5.6M8.8 3.2L3.2 8.8"/></svg>',
  down: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.5v8M4.5 7L8 10.5 11.5 7M3 13.5h10"/></svg>',
  file: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 2h5l3 3v9H4z"/></svg>',
  report: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 2h5l3 3v9H4z"/><path d="M6 8h4M6 10.5h4"/></svg>',
  chev: '<svg class="rp-chev" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3.5 2l3 3-3 3"/></svg>',
};

/** The one-line verdict: {cls: pass|notes|fail, head, sub}. Read from what the run said, never guessed. */
export function verdictOf(opts = {}, fea = null) {
  const outcome = fea && fea.check ? fea.check.outcome : "";
  if (opts.unreviewed) return { cls: "fail", head: "Not signed off.", sub: "The mesh is valid, but the review did not pass it." };
  if (outcome === "fail" || (opts.metrics && opts.metrics.pass === false))
    return { cls: "fail", head: "Failed.", sub: "This mesh did not pass its checks." };
  const notes = (opts.concerns || []).length || (opts.withNotes || []).length
    || outcome === "concerns" || outcome === "unmeasured";
  if (notes) return { cls: "notes", head: "Passed with notes.", sub: "Your mesh is ready." };
  return { cls: "pass", head: "Passed.", sub: "Your mesh is ready." };
}

export function verdictHtml(v) {
  return `<p class="rp-verdict ${v.cls}" role="status"><span class="rp-vm" aria-hidden="true">${ICON[v.cls]}</span>`
    + `<span><b class="v-title">${esc(v.head)}</b> <span class="rp-vsub">${esc(v.sub)}</span></span></p>`;
}

/** "9 min 40 s" from two server times; "" when either is missing. */
export function tookWords(from, to) {
  const a = Date.parse(from || ""), b = Date.parse(to || "");
  if (!Number.isFinite(a) || !Number.isFinite(b) || b < a) return "";
  const s = Math.round((b - a) / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  if (h) return `${h} h ${m % 60} min`;
  return `${m} min${s % 60 ? ` ${s % 60} s` : ""}`;
}

const compact = (n) => !Number.isFinite(n) ? "" : n >= 1e6 ? `${Number((n / 1e6).toPrecision(3))}M`
  : n >= 1e4 ? `${Number((n / 1e3).toPrecision(3))}k` : n.toLocaleString();
const three = (v) => Number(Number(v).toPrecision(3)).toString();

/** The key numbers: [{k, v, sub}] - the size, the worst quality against its floor, the walls, the time. */
export function keyNumbers(surf = {}, opts = {}) {
  const out = [];
  const fea = surf.fea || null, q = surf.quality || {};
  const cells = Number((fea && fea.elements) || surf.cell_count || q.cells || 0);
  if (cells > 0) out.push({ k: fea ? "Elements" : "Cells", v: compact(cells), title: cells.toLocaleString() });
  if (fea) {
    const rows = (fea.check && fea.check.rows) || [];
    // a solid's scaled Jacobian, or a delivered shell's Jacobian ratio (engines/shell_delivery)
    const sj = rows.find((r) => r.key === "fea_min_scaled_jacobian" || r.key === "shell_jacobian_ratio");
    if (sj && typeof sj.measured === "number")
      out.push({ k: "Worst quality", v: three(sj.measured), sub: `needs ${three(sj.limit)}+`, bad: sj.ok === false });
    const tw = fea.through_wall || {};
    if (tw.ok) out.push({ k: "Walls OK", v: "All", sub: tw.min != null ? `${three(tw.min)}+ across` : "" });
    else if ((tw.short || []).length) out.push({ k: "Walls OK", v: `${tw.short.length} thin`, sub: "under 2 across", bad: true });
  } else if (typeof q.max_non_ortho === "number") {
    const lim = surf.quality_fields && surf.quality_fields.metrics && surf.quality_fields.metrics.non_ortho
      && surf.quality_fields.metrics.non_ortho.limit;
    const over = typeof lim === "number" && q.max_non_ortho > lim;
    // just over its limit reads as over: "65.01°", never "65°" beside "limit 65°"
    const v = over && three(q.max_non_ortho) === three(lim) ? Number(q.max_non_ortho.toFixed(2)) : three(q.max_non_ortho);
    out.push({ k: "Worst non-ortho", v: `${v}°`, sub: typeof lim === "number" ? `limit ${three(lim)}°` : "", bad: over });
    if (typeof q.max_skewness === "number") out.push({ k: "Worst skewness", v: three(q.max_skewness) });
  }
  const took = tookWords(opts.createdAt, opts.endedAt);
  if (took) out.push({ k: "Time", v: took });
  return out.slice(0, 4);
}

/** A CFD mesh's figures past their limit, as notes in the server's shape ("title - why: measured x, limit y"). */
export function cfdPastLimit(surf = {}) {
  const q = (surf && surf.quality) || {}, m = surf && surf.quality_fields && surf.quality_fields.metrics;
  const lim = m && m.non_ortho && m.non_ortho.limit;
  if (typeof q.max_non_ortho !== "number" || typeof lim !== "number" || !(q.max_non_ortho > lim)) return [];
  return [`Worst non-orthogonality (degrees) - the solver needs care with these cells: measured ${Number(q.max_non_ortho.toFixed(2))}, limit at most ${three(lim)}`];
}

export function keyNumbersHtml(list) {
  if (!list.length) return "";
  return `<dl class="rp-keys">${list.map((x) => `<div${x.bad ? ' class="bad"' : ""}><dt>${esc(x.k)}</dt>`
    + `<dd${x.title ? ` title="${attr(x.title)}"` : ""}>${esc(x.v)}${x.sub ? `<small>${esc(x.sub)}</small>` : ""}</dd></div>`).join("")}</dl>`;
}

/** A file's label in two parts: its short name, and the caveat the server adds after a dash
 *  ("Nastran bulk data (.bdf) - not yet tested in the real solver") - shown as a small tag and a
 *  tooltip, never as label text. */
export function fileLabel(label) {
  const t = String(label || "").trim();
  const dash = t.indexOf(" - ");
  const name = dash > 0 ? t.slice(0, dash) : t;
  const note = dash > 0 ? t.slice(dash + 3).trim() : "";
  const tag = !note ? "" : /^not yet tested/i.test(note) ? "untested" : /not yet tested/i.test(note) ? "partly tested" : "note";
  return { name, note, tag };
}

/** The downloads: ONE "Download" button that opens a short menu - Everything (the one bundle)
 *  first, then each file by its short name and size, then the Mesh report. ParaView and a thermal
 *  model's files join the same menu when the mesh is read (viewer.js, thermal_results.js). */
export function downloadsHtml(job, files = []) {
  const list = files.filter((f) => f && f.type !== "video");
  const bundle = list.filter((f) => f.type === "mesh_bundle");
  const rest = list.filter((f) => f.type !== "mesh_bundle");
  const row = (f, main) => {
    const l = fileLabel(main ? "" : (f.label || f.type));
    const title = main ? (f.label ? `${f.label}: case folder, solver files, report` : "Case folder, solver files, report") : (l.note || l.name);
    return `<a class="v-dlbtn rp-dl${main ? " rp-all" : ""}" role="menuitem" href="${attr(f.url || "#")}"${downloadAttrs(job, f)}`
      + ` download target="_blank" rel="noopener" title="${attr(title)}">`
      + `${main ? ICON.down : ICON.file}<b>${esc(main ? "Everything" : l.name)}</b>`
      + (l.tag ? `<em class="rp-tag">${esc(l.tag)}</em>` : "")
      + `<span>${fmtBytes(f.size)}</span></a>`;
  };
  const all = bundle[0];
  return `<button type="button" class="rp-dltrig" id="v-dltrig-${attr(job)}" aria-haspopup="menu" aria-expanded="false"`
    + ` aria-controls="v-dlmenu-${attr(job)}">${ICON.down}<b>Download</b>`
    + (all ? `<span>${fmtBytes(all.size)}</span>` : "") + `<i class="rp-dlcaret" aria-hidden="true"></i></button>`
    + `<div class="rp-dlmenu" id="v-dlmenu-${attr(job)}" role="menu" hidden>`
    + bundle.map((f) => row(f, true)).join("") + rest.map((f) => row(f, false)).join("")
    + `<a class="v-dlbtn rp-dl rp-report" role="menuitem" href="#" id="v-report-${attr(job)}" title="Every limit, place, hot spot and check, as a page you can print">`
    + `${ICON.report}<b>Mesh report</b><span>HTML</span></a></div>`;
}

/** The Download button opens and closes its menu; Escape or a click elsewhere closes it. */
export function bindDownloadMenu(root) {
  const trig = root && root.querySelector(".rp-dltrig"), menu = root && root.querySelector(".rp-dlmenu");
  if (!trig || !menu || trig.__menu) return;
  trig.__menu = true;
  const set = (open) => { menu.hidden = !open; trig.setAttribute("aria-expanded", open ? "true" : "false"); };
  trig.addEventListener("click", (e) => { e.stopPropagation(); set(menu.hidden); });
  document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target) && e.target !== trig) set(false); });
  root.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { set(false); trig.focus(); } });
}

/* A NOTE as a title and a short why. The server's words carry a list of places ("measured (1, 2, 3) mm,
   ...") and long reasons after a dash; the places go to the report, the reason is cut to its first part. */
export function noteParts(text) {
  let t = String(text || "").trim().replace(/^important\s*[:-]\s*/i, "").replace(/^reviewer note:\s*/i, "");
  let places = false;
  const m = t.match(/^(.*?):\s*measured\s+(.*?)(?:,\s*limit\s+(.*))?$/i);
  let measured = "";
  if (m) {
    t = m[1];
    if (/\(\s*-?\d/.test(m[2])) places = true; else measured = m[2];
  }
  const dash = t.indexOf(" - ");
  let title = dash > 0 ? t.slice(0, dash) : t, why = dash > 0 ? t.slice(dash + 3) : "";
  why = why.split(/;\s+/)[0];
  if (places) why = (why ? why.replace(/[.\s]+$/, "") + ". " : "") + "Each place is in the Mesh report.";
  title = title.charAt(0).toUpperCase() + title.slice(1);
  if (why) why = why.charAt(0).toUpperCase() + why.slice(1);
  return { title: roundNumbers(title), why: roundNumbers(why), measured: roundNumbers(measured) };
}

/** The Notes dropdown, grouped by kind: [{kind, items: [text]}]. Starts closed. */
export function notesHtml(groups, extra = "") {
  const live = groups.filter((g) => (g.items || []).length);
  const n = live.reduce((a, g) => a + g.items.length, 0);
  if (!n) return "";
  const body = live.map((g) => `<div class="rp-group"><p class="rp-gh">${esc(g.kind)}</p>`
    + g.items.map((t) => {
      const p = noteParts(t);
      return `<div class="rp-kind"><b>${esc(p.title)}</b>${p.measured ? `<span class="rp-c">${esc(p.measured)}</span>` : ""}`
        + (p.why ? `<span class="rp-why">${esc(p.why)}</span>` : "") + `</div>`;
    }).join("") + `</div>`).join("");
  const hint = live.some((g) => g.serious) ? "worth a look" : "nothing to fix";
  return `<details class="rp-drop rp-notes"><summary>${ICON.chev}Notes <span class="rp-n">${n}</span>`
    + `<span class="rp-hint">${hint}</span></summary><div class="rp-body">${body}${extra}</div></details>`;
}

const kindOf = (h) => String(h.shape || h.type || "hot spot");
// "Notch" -> "Notches", "Box" -> "Boxes", "Body" -> "Bodies"; a kind already plural ("Holes") stays
const plural = (k, n) => n === 1 || /s$/.test(k) ? k
  : /(x|z|ch|sh)$/.test(k) ? `${k}es` : /[^aeiou]y$/.test(k) ? `${k.slice(0, -1)}ies` : `${k}s`;

/** Hot spots grouped by kind: [{kind, count, short, ids, why}], the biggest group first. */
export function groupSpots(spots = []) {
  const by = new Map();
  for (const h of spots) {
    const k = kindOf(h);
    if (!by.has(k)) by.set(k, { kind: k, count: 0, short: 0, ids: [], radii: [] });
    const g = by.get(k);
    g.count += 1; g.ids.push(h.id);
    if (h.short) g.short += 1;
    if (typeof h.radius_mm === "number") g.radii.push(h.radius_mm);
  }
  return [...by.values()].sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind)).map((g) => {
    const lo = Math.min(...g.radii), hi = Math.max(...g.radii);
    const size = g.radii.length ? (lo === hi ? `Radius ${three(lo)} mm.` : `Radius ${three(lo)} to ${three(hi)} mm.`) : "";
    const rule = g.short ? `${g.short} short of the finer cells planned.` : "Each one got finer cells.";
    return { kind: g.kind, count: g.count, short: g.short, ids: g.ids, why: [size, rule].filter(Boolean).join(" ") };
  });
}

/** The Hot spots dropdown. Starts closed; a kind steps through its places on the part. */
export function hotSpotsHtml(spots = []) {
  if (!spots.length) return "";
  const groups = groupSpots(spots);
  const short = groups.reduce((a, g) => a + g.short, 0);
  return `<details class="rp-drop rp-spots"><summary>${ICON.chev}Hot spots <span class="rp-n">${spots.length.toLocaleString()}</span>`
    + `<span class="rp-hint">${short ? `${short} short` : "all refined"}</span></summary><div class="rp-body">`
    + groups.map((g) => `<button type="button" class="rp-kind rp-spotkind" data-kind="${attr(g.kind)}">`
      + `<b>${esc(plural(g.kind.charAt(0).toUpperCase() + g.kind.slice(1), g.count))}</b><span class="rp-c">${g.count.toLocaleString()}</span>`
      + `<span class="rp-why">${esc(g.why)}</span><span class="rp-at" aria-live="polite"></span></button>`).join("")
    + `<p class="rp-foot">Click a kind to show its places on the part, one at a time. Every place is in the Mesh report.</p>`
    + `</div></details>`;
}

/** Wire the hot-spot kinds (each click points at the next place of that kind) and the report download. */
export function bindResultPanel(root, job, { spots = [], point = null, isolate = null, fetchReport = null } = {}) {
  if (!root) return null;
  const next = new Map();
  root.querySelectorAll(".rp-spotkind").forEach((btn) => {
    const list = spots.filter((h) => kindOf(h) === btn.dataset.kind);
    btn.onclick = () => {
      const i = next.get(btn.dataset.kind) || 0, h = list[i % list.length];
      next.set(btn.dataset.kind, i + 1);
      root.querySelectorAll(".rp-spotkind").forEach((b) => b.classList.toggle("on", b === btn));
      root.querySelectorAll(".rp-spotkind .rp-at").forEach((s) => { s.textContent = ""; });
      btn.querySelector(".rp-at").textContent = `${(i % list.length) + 1} of ${list.length}`;
      if (isolate) isolate((h.faces || [])[0] || null);
      if (point) point(h.at_m || null);
    };
  });
  const rep = root.querySelector(`#v-report-${CSS.escape(String(job))}`);
  if (rep && fetchReport) {
    rep.onclick = async (e) => {
      e.preventDefault();
      if (rep.dataset.busy) return;
      rep.dataset.busy = "1";
      const lbl = rep.querySelector("span"), was = lbl.textContent;
      lbl.textContent = "preparing…";
      try {
        const blob = await fetchReport(job);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a"); a.href = url; a.download = `mesh_report_${String(job).slice(0, 8)}.html`;
        document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
        lbl.textContent = fmtBytes(blob.size);
      } catch (err) {
        lbl.textContent = "not available";
        console.warn("mesh report", err);
      } finally {
        delete rep.dataset.busy;
        setTimeout(() => { lbl.textContent = was; }, 4000);
      }
    };
  }
  return { reset: () => { next.clear(); root.querySelectorAll(".rp-spotkind").forEach((b) => b.classList.remove("on")); } };
}
