// Console revamp mockups - the shared shell: the left pane (Runs or Files) and the viewer toolbar.
const S = 'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"';
const I = {
  panel: `<svg viewBox="0 0 16 16" ${S}><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M6 3v10"/></svg>`,
  plus: `<svg viewBox="0 0 16 16" ${S}><path d="M8 3v10M3 8h10"/></svg>`,
  search: `<svg viewBox="0 0 16 16" ${S}><circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2L13.5 13.5"/></svg>`,
  runs: `<svg viewBox="0 0 16 16" ${S}><path d="M3 4.5h10M3 8h10M3 11.5h6"/></svg>`,
  usage: `<svg viewBox="0 0 16 16" ${S}><path d="M3 13V9M6.5 13V6M10 13V8M13.5 13V3.5"/></svg>`,
  settings: `<svg viewBox="0 0 16 16" ${S}><circle cx="8" cy="8" r="2"/><path d="M8 2.5v1.6M8 11.9v1.6M2.5 8h1.6M11.9 8h1.6M4.1 4.1l1.1 1.1M10.8 10.8l1.1 1.1M4.1 11.9l1.1-1.1M10.8 5.2l1.1-1.1"/></svg>`,
  folder: `<svg viewBox="0 0 16 16" ${S}><path d="M2 4.5h4l1.3 1.5H14v6.5H2z"/></svg>`,
  file: `<svg viewBox="0 0 16 16" ${S}><path d="M4 2h5l3 3v9H4z"/></svg>`,
  cube: `<svg viewBox="0 0 16 16" ${S}><path d="M8 1.8l5.5 3.1v6.2L8 14.2l-5.5-3.1V4.9z"/><path d="M2.5 4.9L8 8l5.5-3.1M8 8v6.2"/></svg>`,
  cut: `<svg viewBox="0 0 16 16" ${S}><path d="M3 11l6-8 4 3-6 8z" opacity=".5"/><path d="M1.5 9.5l13-3"/></svg>`,
  layers: `<svg viewBox="0 0 16 16" ${S}><path d="M8 2.5l6 3-6 3-6-3z"/><path d="M2 8.5l6 3 6-3"/><path d="M2 11.2l6 3 6-3" opacity=".6"/></svg>`,
  ruler: `<svg viewBox="0 0 16 16" ${S}><path d="M2.5 10.5l8-8 3 3-8 8z"/><path d="M5 8l1.3 1.3M7 6l1.3 1.3M9 4l1.3 1.3"/></svg>`,
  camera: `<svg viewBox="0 0 16 16" ${S}><path d="M2 5.5h2.6L6 3.8h4l1.4 1.7H14v7H2z"/><circle cx="8" cy="9" r="2.2"/></svg>`,
  caret: `<svg class="caret" viewBox="0 0 8 8" ${S}><path d="M1.5 3l2.5 2.5L6.5 3"/></svg>`,
  hex: `<svg viewBox="0 0 16 16" fill="none" stroke="var(--accent)" stroke-width="1.6"><path d="M8 1.6l5.5 3.2v6.4L8 14.4l-5.5-3.2V4.8z"/><path d="M8 5.3l2.3 1.35v2.7L8 10.7 5.7 9.35v-2.7z" fill="var(--accent)" stroke="none"/></svg>`,
};

const runsList = (cur) => [
  ["live", "Demo pump housing", "now"], ["warn", "Demo bracket, FEA", "2 h"], ["ok", "Demo duct, CFD", "1 d"],
  ["ok", "Demo heat sink", "2 d"], ["bad", "Demo manifold", "3 d"], ["ok", "Demo wing section", "5 d"],
].map(([d, t, a], i) => `<a class="run" href="#"${i === cur ? ' aria-current="page"' : ""}><span class="run-dot ${d}"></span>
  <span class="t">${t}</span><span class="age">${a}</span></a>`).join("");

function nav(cur) {
  return `<div class="nav">
    <a class="new" href="#">${I.plus}New run<span class="kbd">N</span></a>
    <a href="#">${I.search}Search<span class="kbd">/</span></a>
    <a href="#">${I.runs}All runs</a>
    <a href="#">${I.usage}Usage</a>
  </div>
  <div class="group"><span>Recent</span></div>
  <div class="runs">${runsList(cur)}</div>`;
}

/* only what the user takes away - never the workspace */
function files(stage) {
  const row = (n, s, cls = "") => `<div class="f-row ${cls}">${I.file}<span class="n">${n}</span><span class="s">${s}</span></div>`;
  const live = stage === "live";
  return `<div class="files">
    <div class="f-folder">${I.folder}<span>Your upload</span></div>
    ${row("pump_housing.step", "14.2 MB")}
    <div class="f-folder" style="margin-top:8px">${I.folder}<span>Case folder</span></div>
    ${row("constant/polyMesh", live ? "writing" : "212 MB", live ? "pending" : "")}
    ${row("system/", live ? "3 files" : "6 files", live ? "new" : "")}
    ${live ? row("0/  (starting values)", "soon", "pending") : row("0/", "5 files")}
    <div class="f-folder" style="margin-top:8px">${I.folder}<span>For your solver</span></div>
    ${live ? row("Solver files", "after checks", "pending") : row("fluent.msh", "188 MB") + row("starccm.ccm", "201 MB")}
    <div class="f-folder" style="margin-top:8px">${I.folder}<span>Report</span></div>
    ${live ? row("Mesh report", "at the end", "pending") : row("mesh_report.pdf", "1.4 MB", "new")}
  </div>
  <p class="f-note">${live ? "Files show up here as Hexera writes them." : "Everything above is in the one zip."}</p>`;
}

document.querySelectorAll("[data-pane]").forEach((el) => {
  const mode = el.dataset.pane, cur = +(el.dataset.cur || 0);
  el.innerHTML = `<div class="pane-top"><span class="brand"><span class="brand-mark">${I.hex}</span>HEXERA</span>
      <span class="spacer"></span><button class="icon-btn" aria-label="Hide side panel">${I.panel}</button></div>
    <div class="seg" role="group" aria-label="Side panel">
      <button aria-pressed="${mode === "nav"}">Runs</button>
      <button aria-pressed="${mode !== "nav"}">Files ${mode === "nav" ? "" : `<span class="count">${el.dataset.stage === "live" ? "4" : "9"}</span>`}</button></div>
    ${mode === "nav" ? nav(cur) : files(el.dataset.stage)}
    <div class="pane-foot"><span class="avatar">A</span><span>Areen</span><span class="credits">420 credits</span>
      <button class="icon-btn" aria-label="Settings">${I.settings}</button></div>`;
});

document.querySelectorAll("[data-vt]").forEach((el) => {
  const open = el.dataset.vt;
  const b = (k, icon, label, dd = true) => `<button aria-expanded="${open === k}"${dd ? ' aria-haspopup="dialog"' : ""}>${I[icon]}${label}${dd ? I.caret : ""}</button>`;
  el.innerHTML = b("view", "cube", "View") + b("section", "cut", "Section") + b("parts", "layers", "Parts")
    + '<span class="sep"></span>' + b("measure", "ruler", "Measure", false) + b("snap", "camera", "Snapshot");
});

