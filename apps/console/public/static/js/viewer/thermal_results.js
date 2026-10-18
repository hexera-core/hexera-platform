// Responsibility: Show a delivered thermal model's mesh the way an electronics-cooling engineer reads it:
// every part by the file's name with its cells, the cells through its thinnest layer and its volume
// against the file; the quality in plain words; ready-made slices; and the per-part table and the
// thermal model as downloads.
// Boundaries: markup and the panel's own controls. It draws nothing on the 3D canvas itself - the
// viewer hands it `hooks` (isolate a part, show a slice, show the whole model) and owns the actors.
// Collaborates with: viewer/viewer.js (mounts it when the payload carries `thermal`),
// engines/snapgrid/results.py (builds the block it shows).
import { esc, fmtBytes } from "../core/format.js";

/* the table's columns: [key, heading, numeric] */
const COLS = [["label", "Part", false], ["cells", "Cells", true], ["thinnest_mm", "Thinnest", true],
              ["cells_through", "Cells through", true], ["volume_vs_file_pct", "Volume vs file", true]];
/* above this many parts the table gets a search box (a package has hundreds of solder balls) */
const SEARCH_FROM = 30;
/* A SLICE'S COLOURS: each part its own muted colour, in the file's order, so the parts in one cut
   read apart; the air a quiet grey under them */
const SLICE_COLS = [[0.86, 0.74, 0.52], [0.52, 0.74, 0.69], [0.82, 0.57, 0.49], [0.60, 0.67, 0.86],
                    [0.77, 0.64, 0.82], [0.69, 0.80, 0.53], [0.88, 0.62, 0.67], [0.56, 0.79, 0.86],
                    [0.86, 0.80, 0.58], [0.66, 0.60, 0.79]];
const AIR_COL = [0.40, 0.45, 0.51];

/** The colour a part (or the air) is drawn in on a slice. */
export function sliceColour(t, region) {
  if ((t && t.air || []).some((a) => a.name === region)) return AIR_COL;
  const i = (t && t.parts || []).findIndex((p) => p.name === region);
  return SLICE_COLS[(i < 0 ? 0 : i) % SLICE_COLS.length];
}
const rgb = (c) => `rgb(${c.map((v) => Math.round(v * 255)).join(",")})`;

const num = (v) => (v === null || v === undefined || v === "" || isNaN(v)) ? null : Number(v);
const fmtNum = (v) => { const n = num(v); return n === null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: 4 }); };

function throughCell(r) {
  const n = num(r.cells_through);
  if (n === null) return `<td class="n">—</td>`;
  const asked = num(r.cells_asked);
  const short = r.layer_ok === false;
  return `<td class="n${short ? " short" : ""}" title="${asked !== null ? `asked for ${asked}` : ""}">`
    + `${n}${asked !== null ? `<small> of ${asked}</small>` : ""}</td>`;
}

function rowHtml(r) {
  const vol = r.volume_match || "—";
  return `<tr class="tr-row" data-part="${esc(r.name)}" tabindex="0">`
    + `<td class="nm"><span>${esc(r.label || r.name)}</span>`
    + `<small>${esc([r.kind, r.material, r.power_W ? `${Number(r.power_W).toLocaleString(undefined, { maximumFractionDigits: 3 })} W` : ""]
        .filter(Boolean).join(" · "))}</small></td>`
    + `<td class="n">${fmtNum(r.cells)}</td>`
    + `<td class="n">${r.thinnest_text ? esc(r.thinnest_text) : fmtNum(r.thinnest_mm)}</td>`
    + throughCell(r)
    + `<td class="n${vol === "exact" ? " ok" : ""}">${esc(vol)}</td></tr>`;
}

/** The layers carried as thin-layer resistances (no cells of their own), one line each. */
function thinLayersHtml(rows) {
  if (!rows.length) return "";
  const r3 = (v) => Number(Number(v).toPrecision(3)).toExponential().replace(/\.?0+e/, "e");
  return `<div class="v-sec tr-thin">
      <div class="v-sec-h">Thin layers carried as resistances <span class="tr-n">${rows.length.toLocaleString()}</span></div>
      <ul class="tr-q">${rows.map((f) => `<li><b>${esc(f.label)}</b>, ${esc(f.thickness_text)}`
        + `${f.material ? ` of ${esc(f.material)}` : ""}: R = ${esc(r3(f.R_m2K_W))} m²K/W between `
        + `${esc((f.between || []).join(" and "))}`
        + `${(f.patches || []).length ? ` <small>(${esc(f.patches.slice(0, 3).join(", "))}${f.patches.length > 3 ? ", …" : ""})</small>` : ""}</li>`).join("")}</ul>
    </div>`;
}

/** The panel section for one delivered thermal model (`t` = the payload's `thermal` block). */
export function thermalResultsHtml(job, t) {
  if (!t) return "";
  const parts = t.parts || [], air = t.air || [], slices = t.slices || [];
  const by = t.producer ? ` <span class="tr-by">from ${esc(t.producer)}</span>` : "";
  const airCells = air.reduce((s, a) => s + (Number(a.cells) || 0), 0);
  return `<div class="v-sec tr" id="v-thermal-sec-${esc(job)}">
      <div class="v-sec-h">Thermal model · ${esc(t.title || "")}${by}</div>
      <div class="tr-sum">${esc(t.summary || "")}</div>
      <ul class="tr-q">${(t.quality || []).map((q) => `<li>${esc(q)}</li>`).join("")}</ul>
    </div>
    ${slices.length ? `<div class="v-sec tr-slices">
      <div class="v-sec-h">Slices</div>
      <div class="tr-sl-list">
        <button class="tr-sl on" data-slice="" type="button">Whole model</button>
        ${slices.map((s) => `<button class="tr-sl" data-slice="${esc(s.name)}" type="button">${esc(s.label)}</button>`).join("")}
      </div>
      <div class="tr-sl-why" id="v-thermal-why-${esc(job)}"></div>
    </div>` : ""}
    ${thinLayersHtml(t.thin_layers || [])}
    <div class="v-sec tr-parts">
      <div class="v-sec-h">Parts <span class="tr-n">${parts.length.toLocaleString()}</span></div>
      ${parts.length > SEARCH_FROM ? `<input class="tr-find" type="search" placeholder="Find a part or material" aria-label="Find a part">` : ""}
      <div class="tr-wrap"><table class="tr-tab">
        <thead><tr>${COLS.map(([k, h, n]) => `<th data-k="${k}"${n ? ' class="n"' : ""}>${h}</th>`).join("")}</tr></thead>
        <tbody>${parts.map(rowHtml).join("")}</tbody>
      </table></div>
      ${air.length ? `<div class="tr-air">${air.length > 1 ? `${air.length} air spaces` : "Air"}: `
        + `${airCells.toLocaleString()} cells, hidden on the model so the parts show.</div>` : ""}
      <div class="tr-hint">Click a part to show it alone; click it again for the whole model.</div>
    </div>`;
}

function download(name, text, type) {
  const blob = new Blob([text], { type }), url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/** The thermal model's own files beside the case download: the per-part table and the model. */
export function thermalDownloads(bar, job, t) {
  if (!bar || !t || !t.files) return [];
  const stem = String(t.title || "thermal_model").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "thermal_model";
  const made = [];
  const add = (id, label, file, text, type) => {
    if (!text) return;
    const a = document.createElement("a"); a.className = "v-dlbtn"; a.href = "#"; a.id = id;
    a.innerHTML = `<b>${esc(label)}</b><span>${esc(file.split(".").pop())} · ${fmtBytes(new Blob([text]).size)}</span>`;
    a.title = file;
    a.onclick = (e) => { e.preventDefault(); download(file, text, type); };
    bar.appendChild(a); made.push(a);
  };
  add(`v-csv-${job}`, "Per-part table", `${stem}_per_part.csv`, t.files.per_part_csv, "text/csv");
  add(`v-tm-${job}`, "Thermal model", `${stem}.thermal_model.json`, t.files.thermal_model_json, "application/json");
  return made;
}

/** Wire the section: sorting, the search, a part shown alone, a slice. `hooks` =
    {isolate(name|null), slice(name|null)}; each returns nothing. */
export function bindThermalResults(root, job, t, hooks) {
  if (!root || !t) return;
  const body = root.querySelector(".tr-tab tbody");
  const rows = (t.parts || []).slice();
  let sortKey = null, asc = true, picked = null, query = "";
  const draw = () => {
    let shown = rows;
    if (query) shown = shown.filter((r) => `${r.label} ${r.name} ${r.material} ${r.kind}`.toLowerCase().includes(query));
    if (sortKey) {
      const col = COLS.find((c) => c[0] === sortKey);
      shown = shown.slice().sort((a, b) => {
        const x = a[sortKey], y = b[sortKey];
        let d;
        if (col && col[2]) d = (num(x) ?? -Infinity) - (num(y) ?? -Infinity);
        else d = String(x || "").localeCompare(String(y || ""), undefined, { numeric: true });
        return asc ? d : -d;
      });
    }
    body.innerHTML = shown.map(rowHtml).join("");
    body.querySelectorAll(".tr-row").forEach((tr) => {
      tr.classList.toggle("sel", tr.dataset.part === picked);
      const pick = () => {
        picked = picked === tr.dataset.part ? null : tr.dataset.part;
        if (hooks && hooks.slice) setSlice("");
        if (hooks && hooks.isolate) hooks.isolate(picked);
        draw();
      };
      tr.onclick = pick;
      tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } };
    });
    root.querySelectorAll(".tr-tab th").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.k === sortKey);
      th.classList.toggle("desc", th.dataset.k === sortKey && !asc);
    });
  };
  root.querySelectorAll(".tr-tab th").forEach((th) => {
    th.onclick = () => {
      if (sortKey === th.dataset.k) asc = !asc;
      else { sortKey = th.dataset.k; asc = !(COLS.find((c) => c[0] === sortKey) || [])[2]; }
      draw();
    };
  });
  const find = root.querySelector(".tr-find");
  if (find) find.oninput = () => { query = find.value.trim().toLowerCase(); draw(); };
  const why = root.querySelector(`#v-thermal-why-${CSS.escape(String(job))}`);
  function setSlice(name) {
    root.querySelectorAll(".tr-sl").forEach((b) => b.classList.toggle("on", b.dataset.slice === name));
    const s = (t.slices || []).find((x) => x.name === name);
    if (!why) return;
    if (!s) { why.innerHTML = ""; return; }
    // what is in this cut, each in the colour it is drawn in
    const seen = [];
    (s.pieces || []).forEach((pc) => { if (!seen.includes(pc.region)) seen.push(pc.region); });
    const label = (r) => ((t.parts || []).find((p) => p.name === r) || {}).label
      || ((t.air || []).find((a) => a.name === r) || {}).label || r;
    why.innerHTML = `<div>Shows ${esc(s.why)}.</div><div class="tr-legend">`
      + seen.map((r) => `<span><i style="background:${rgb(sliceColour(t, r))}"></i>${esc(label(r))}</span>`).join("")
      + `</div>`;
  }
  root.querySelectorAll(".tr-sl").forEach((b) => {
    b.onclick = () => {
      const name = b.dataset.slice || "";
      setSlice(name);
      if (name && picked) { picked = null; if (hooks && hooks.isolate) hooks.isolate(null); draw(); }
      if (hooks && hooks.slice) hooks.slice(name || null);
    };
  });
  draw();
  return { pick: (name) => { picked = name; draw(); }, slice: (name) => setSlice(name || "") };
}
