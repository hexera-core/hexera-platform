/* Responsibility: Show the customer what the CAD repair did, whenever a repair ran - the repaired part in 3D with its
   problems pinned, the key numbers, the plain trace of every problem and the repair's files - and, for an older run
   with no repair section, hand over the repaired file on any ending. */
/* Boundaries: it renders what the API delivered (the job's typed `repair` section and its artifacts; a Download
   click fetches a fresh link through render/download.js). Every sentence comes from core/repair_words.js; the 3D
   scene is viewer/repair_view.js. It decides no status and makes no claim about the mesh. */

import { getGeometrySkin, getJob, getRepairReport, getRepairSurfaces } from "../api/endpoints.js";
import { repairOf } from "../realtime/stream.js";
import { esc, fmtBytes } from "../core/format.js";
import { byType, countOf, flagLines, headlineOf, repairFiles, repairKeyNumbers, traceRows, traceSentence }
  from "../core/repair_words.js";
import { keyNumbersHtml, verdictHtml } from "../viewer/result_panel.js";
import { bindFreshDownloads, downloadAttrs } from "./download.js";

/* WHY THIS IS NOT PART OF THE VIEWER. The viewer is the mesh's surface, and it only opens on
   proof that a mesh passed a gate (core/events.js resultSurface). A repair is a DIFFERENT
   deliverable with its own fate: the geometry can be fixed and the mesh still fail. Hanging the
   repaired file off the viewer meant a repair we had already done, and already stored, was
   withheld from the person who paid for it because a later stage went wrong. */

/* The repair's own deliverables, in the order a customer uses them (core/repair_words.js REPAIR_FILES): the
   fixed geometry first, the customer's own file next, the explanation after. Any other class belongs to another
   panel. */
export function repairDeliverables(files) {
  return repairFiles(files);
}

const attr = (s) => esc(s).replace(/"/g, "&quot;");

/* THE REPORT, read from the run's own routes (/repair/report.html and .json) when it was not delivered as a file:
   the same two entries either way, saved by a click as the mesh report is. */
const REPORT_ROUTES = [["repair_report_html", "html", "Repair report (HTML)"],
  ["repair_report", "json", "Repair report (JSON)"]];
function reportButtons(files) {
  return REPORT_ROUTES.filter(([t]) => !files.some((f) => f.type === t)).map(([, kind, name]) =>
    `<a class="v-dlbtn rr-rep" href="#" role="button" data-kind="${kind}"><b>${esc(name)}</b>`
    + `<span>${kind.toUpperCase()}</span></a>`).join("");
}
function bindReports(box, job) {
  box.querySelectorAll(".rr-rep").forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      if (a.dataset.busy) return;
      a.dataset.busy = "1";
      const lbl = a.querySelector("span"), was = lbl.textContent;
      lbl.textContent = "preparing…";
      try {
        const blob = await getRepairReport(job, a.dataset.kind);
        const url = URL.createObjectURL(blob), link = document.createElement("a");
        link.href = url;
        link.download = `repair_report_${String(job).slice(0, 8)}.${a.dataset.kind}`;
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        lbl.textContent = was;
      } catch {
        lbl.textContent = "not available";
        setTimeout(() => { lbl.textContent = was; }, 4000);
      } finally { delete a.dataset.busy; }
    };
  });
}

function downloadsHtml(job, files, { reports = false } = {}) {
  const extra = reports ? reportButtons(files) : "";
  if (!files.length && !extra) return '<p class="rr-none">No repair files were kept for this run.</p>';
  return files.map((f, i) => {
    // The repaired geometry leads in the accent, the way the mesh case does in the viewer: it is
    // the thing the customer came for.
    const primary = i === 0 && f.type === "repaired_cad" ? " primary" : "";
    return `<a class="v-dlbtn${primary}" href="${attr(f.url || "#")}"${downloadAttrs(job, f)} download
               target="_blank" rel="noopener"><b>${esc(f.name || f.label || "File")}</b
               ><span>${fmtBytes(f.size)}</span></a>`;
  }).join("") + extra;
}

/** The problems by plain type, one line each ("3 thin gaps: 2 fixed"). */
export function typesHtml(r) {
  const rows = byType(r);
  if (!rows.length) return "";
  return `<ul class="rr-types">${rows.map((t) => `<li><b>${esc(countOf(t.type, t.found, t.typeText))}</b>`
    + `<span>${t.fixed === t.found ? "all fixed" : `${t.fixed} fixed`}</span></li>`).join("")}</ul>`;
}

/** The plain trace: problem -> what we did -> result, a row per problem, each a button that looks at its pin. */
export function traceHtml(r) {
  const rows = traceRows(r);
  if (!rows.length) return "";
  return `<details class="rp-drop rr-trace-drop"${rows.length <= 12 ? " open" : ""}><summary>`
    + `<svg class="rp-chev" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3.5 2l3 3-3 3"/></svg>`
    + `What we did <span class="rp-n">${rows.length}</span></summary>`
    + `<ol class="rr-trace">${rows.map((t) => `<li class="${t.status}"><button type="button" class="rr-go" data-id="${attr(t.id)}"`
      + ` aria-label="Look at problem ${t.n}"><span class="rr-n">${t.n}</span>`
      + `<i class="rpv-dot ${t.status}" aria-hidden="true"></i><span class="rr-t">${esc(traceSentence(t))}</span></button></li>`).join("")}`
    + `</ol></details>`;
}

function flagsHtml(r) {
  const lines = flagLines(r);
  if (!lines.length) return "";
  return `<ul class="rr-flags">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
}

/** The whole side panel, as HTML: headline, key numbers, flags, files, problems by type and the trace. */
export function repairResultHtml(job, data) {
  const r = data.repair || {};
  const files = repairFiles(data.files);
  const alongside = r.choice !== "standalone" && data.pass;
  return `<div class="v-bar">
      <div class="v-sec v-head rp-head">
        <p class="rr-eyebrow">CAD repair${alongside ? " · used for your mesh" : ""}</p>
        ${verdictHtml(headlineOf(r, { hasFile: files.some((f) => f.type === "repaired_cad"), meshed: !!data.pass }))}
        <div class="rp-keys-host">${keyNumbersHtml(repairKeyNumbers(r))}</div>
        ${flagsHtml(r)}
      </div>
      <div class="v-sec rr-dl"><div class="v-sec-h">Files</div><div class="v-dl">${downloadsHtml(job, files, { reports: r.ran !== false })}</div></div>
      ${typesHtml(r) ? `<div class="v-sec"><div class="v-sec-h">Problems found</div>${typesHtml(r)}</div>` : ""}
      ${traceHtml(r) ? `<div class="v-sec rp-lists">${traceHtml(r)}</div>` : ""}
    </div>
    <div class="v-canvas rr-canvas" id="rr-canvas-${attr(job)}">
      <div class="v-loading" id="rr-load-${attr(job)}"><div>Loading your part…</div></div>
    </div>`;
}

/** THE JOB READ AGAIN, once, as the panel opens: the run's ending can reach the page before the repair's own
 *  section and files are written beside it (seen on slug crsv5ui: a "ran: false" section and no files at the
 *  ending, the whole section and the report a moment later). Answers the newer data, or the data it was given. */
async function freshData(job, data, read = getJob) {
  const j = await read(job, { throwOnError: false }).catch(() => null);
  const repair = j && repairOf(j);
  if (!repair) return data;
  const files = (j.artifacts || []).map((a) => ({ id: a.id, type: a.artifact_type, label: a.label,
    size: a.size_bytes, url: a.download_url }));
  return { ...data, repair, files: files.length ? files : data.files };
}

/** The surfaces the view draws: the repair's own (original and repaired), else the checked part alone. */
async function surfacesFor(job, data) {
  const s = await getRepairSurfaces(job).catch(() => null);
  if (s && (s.repaired || s.original)) return s;
  if (!data.sessionId) return null;
  const skin = await getGeometrySkin(data.sessionId).catch(() => null);
  return skin ? { original: skin } : null;
}

/**
 * Draw the repair result for a run whose job carries a `repair` section. For "Repair CAD only" it is the run's
 * result and takes the stage the way a delivered mesh does; beside a mesh it sits in the conversation, under the
 * run. Idempotent in the job id. Returns the element.
 */
export function openRepairResult(job, anchorEl, data, { takeover = false, mount = null, tabWith = null, read = getJob } = {}) {
  const existing = document.getElementById("repair-" + job);
  if (existing) return existing;
  const box = document.createElement("div");
  box.className = "viewer rr-result";
  box.id = "repair-" + job;
  box.setAttribute("aria-label", "CAD repair result");
  box.innerHTML = repairResultHtml(job, data);
  bindFreshDownloads(box);
  bindReports(box, job);
  const wb = document.getElementById("workbench"), app = document.getElementById("app");
  if (tabWith && tabWith.parentNode) {
    // BESIDE A DELIVERED MESH: one "Mesh | Repair" switch over the two results, in the place the mesh took. The
    // hidden tab's scene starts the first time it is shown (the observer below waits for it to be on screen).
    tabWith.after(box);
    box.hidden = true;
    resultTabs(job, tabWith, box);
  } else if (takeover && wb && app) {
    box.setAttribute("role", "region");
    wb.querySelectorAll(".viewer").forEach((v) => { v.hidden = true; });
    wb.appendChild(box);
    app.classList.add("wb"); app.classList.remove("wb-collapsed");
  } else if (anchorEl) { box.setAttribute("role", "region"); anchorEl.after(box); }
  else return null;

  const host = box.querySelector(".rr-canvas");
  let view = null;
  const go = (id) => { if (view) view.focus(id); };
  const bindBar = () => box.querySelectorAll(".rr-go").forEach((b) => { b.onclick = () => go(b.dataset.id); });
  bindBar();
  host.addEventListener("repair-focus", (e) => {
    box.querySelectorAll(".rr-go").forEach((b) => b.classList.toggle("on", b.dataset.id === e.detail.id));
  });
  // the scene is read once the panel is on screen, as the mesh viewer's is
  const start = async () => {
    // the side panel follows the job as it now stands, when that differs from what the ending carried
    const now = read ? await freshData(job, data, read) : data;
    if (now !== data && JSON.stringify([now.repair, now.files]) !== JSON.stringify([data.repair, data.files])) {
      data = now;
      const bar = document.createElement("div");
      bar.innerHTML = repairResultHtml(job, data);
      box.querySelector(".v-bar").replaceWith(bar.querySelector(".v-bar"));
      bindFreshDownloads(box); bindReports(box, job); bindBar();
    }
    try {
      const surfaces = await surfacesFor(job, data);
      const mountView = mount || (await import("../viewer/repair_view.js")).mountRepairView;
      view = surfaces ? await mountView(host, job, data.repair || {}, surfaces) : null;
      const l = document.getElementById("rr-load-" + job);
      if (view) l?.remove();
      else if (l) l.innerHTML = "<div>The 3D view isn't available for this run. The files and the list still are.</div>";
    } catch {
      const l = document.getElementById("rr-load-" + job);
      if (l) l.innerHTML = "<div>The 3D view could not load. The files still work.</div>";
    }
  };
  if (typeof IntersectionObserver === "function") {
    const io = new IntersectionObserver((es) => { if (es.some((x) => x.isIntersecting)) { io.disconnect(); start(); } },
      { threshold: 0.05 });
    io.observe(box);
  } else start();
  return box;
}

/** The two results' switch: a tab list before them; each tab shows its result and hides the other. The arrow keys
 *  move between the tabs, as a tab list's do. Returns the tab list. */
export function resultTabs(job, meshBox, repairBox) {
  const bar = document.createElement("div");
  bar.className = "rr-tabs";
  bar.id = "rr-tabs-" + job;
  bar.setAttribute("role", "tablist");
  bar.setAttribute("aria-label", "Results");
  if (!meshBox.id) meshBox.id = "viewer-" + job;
  bar.innerHTML = [["mesh", "Mesh", meshBox.id], ["repair", "Repair", repairBox.id]].map(([k, l, id], i) =>
    `<button type="button" role="tab" class="rr-tab${i ? "" : " on"}" data-k="${k}" id="rr-tab-${k}-${attr(job)}"`
    + ` aria-controls="${attr(id)}" aria-selected="${i ? "false" : "true"}" tabindex="${i ? -1 : 0}">${l}</button>`).join("");
  meshBox.before(bar);
  [meshBox, repairBox].forEach((b, i) => {
    b.setAttribute("role", "tabpanel");
    b.setAttribute("aria-labelledby", `rr-tab-${i ? "repair" : "mesh"}-${job}`);
  });
  const pick = (k, focus) => {
    bar.querySelectorAll(".rr-tab").forEach((t) => {
      const on = t.dataset.k === k;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    meshBox.hidden = k !== "mesh";
    repairBox.hidden = k !== "repair";
  };
  bar.querySelectorAll(".rr-tab").forEach((t) => {
    t.onclick = () => pick(t.dataset.k);
    t.onkeydown = (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      pick(t.dataset.k === "mesh" ? "repair" : "mesh", true);
    };
  });
  return bar;
}

/** Draw the repair panel at `anchorEl` for a run with no typed repair section (an older run), or draw nothing and
 *  return null if nothing was repaired.
 *
 *  Returns the element so a caller can scroll to it. Idempotent in the job id: a re-render or a
 *  reconnect must not stack two panels on one run.
 */
export function openRepairPanel(job, anchorEl, data) {
  if (data && data.repair) return openRepairResult(job, anchorEl, data);
  const files = repairDeliverables((data || {}).files);
  if (!files.length || !anchorEl) return null;
  const existing = document.getElementById("repair-" + job);
  if (existing) return existing;

  const box = document.createElement("div");
  box.className = "viewer";
  box.id = "repair-" + job;
  // WHAT THE HEADLINE MAY CLAIM. Only that the geometry was repaired, which is what the presence
  // of a `repaired_cad` row proves. It says nothing about the mesh: on this path there isn't one,
  // and the outcome message above has already explained why.
  const repaired = files.some((f) => f.type === "repaired_cad");
  const headline = repaired
    ? "CAD Repair System repaired your CAD"
    : "What CAD Repair System found in your CAD";
  const line = repaired
    ? "Your original file is unchanged - the repair is a new file, and the report says what "
      + "was done to it."
    : "The inspection could not repair this automatically. The report says what it found.";
  // The panel borrows the result panel's own sections rather than inventing a look: a head with
  // the title, the explanation in the `v-why` band that wraps, and the downloads as a `v-dl`
  // column. `v-status` is deliberately NOT used for the prose - it is a single ellipsised line.
  box.innerHTML = `<div class="v-bar">
      <div class="v-sec v-head">
        <div class="v-row v-titlerow"><b class="v-title">${esc(headline)}</b></div>
      </div></div>
    <div class="v-why">${esc(line)}</div>
    <div class="v-sec"><div class="v-dl">${downloadsHtml(job, files)}</div></div>`;
  bindFreshDownloads(box);
  anchorEl.appendChild(box);
  return box;
}
