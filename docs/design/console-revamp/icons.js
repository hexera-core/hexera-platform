// Console revamp mockups - the status icons and the live counters.
// Each icon is original, drawn on an 18px grid in currentColor; the moving part is marked
// `live` and coloured by --work-live in mock.css. Fills any <span class="ic" data-ic="...">.
const A = 'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"';
const hex = (cx, cy, r = 3) => [[r, 0], [r / 2, r * .866], [-r / 2, r * .866], [-r, 0], [-r / 2, -r * .866], [r / 2, -r * .866]]
  .map(([x, y]) => `${(cx + x).toFixed(1)},${(cy + y).toFixed(1)}`).join(" ");

const ICONS = {
  // reading your CAD: a page, a scan line walking down it
  read: `<svg class="ic-read" viewBox="0 0 18 18" ${A}><path d="M4 2.5h6.5L14 6v9.5H4z"/><path d="M10.5 2.5V6H14"/>
    <path class="live scan" d="M6 6.5h6"/></svg>`,
  // measuring walls: a caliper, one jaw moving
  measure: `<svg class="ic-measure" viewBox="0 0 18 18" ${A}><path d="M2.5 4h13"/><path d="M4 4v10"/>
    <path d="M13 4v1.6M15 4v1.6"/><path class="live jaw" d="M8.5 4v7"/></svg>`,
  // refining: a hexagon that splits into a finer one
  refine: `<svg class="ic-refine" viewBox="0 0 18 18" ${A}><polygon points="${hex(9, 9, 6.5)}"/>
    <polygon class="live inner" points="${hex(9, 9, 6.5)}"/></svg>`,
  // boundary layers: thin layers growing off a wall
  layer: `<svg class="ic-layer" viewBox="0 0 18 18" ${A}><path d="M2.5 15h13" stroke-width="2"/>
    <path class="live l l1" d="M3 12.3h12"/><path class="live l l2" d="M3 9.6h12"/><path class="live l l3" d="M3 6.9h12"/></svg>`,
  // filling the volume: hex cells light up in turn
  fill: `<svg class="ic-fill" viewBox="0 0 18 18" ${A} stroke-width="1.1"><polygon class="c c1" points="${hex(5.2, 5.5)}"/>
    <polygon class="c c2" points="${hex(9.7, 8.1)}"/><polygon class="c c3" points="${hex(5.2, 10.7)}"/>
    <polygon class="c c4" points="${hex(14.2, 5.5)}"/></svg>`,
  // checking quality: a gauge needle settling
  check: `<svg class="ic-check" viewBox="0 0 18 18" ${A}><path d="M3.5 13a5.5 5.5 0 0 1 11 0"/>
    <path class="live needle" d="M9 12.5V7.4"/><circle cx="9" cy="12.6" r="1" fill="currentColor" stroke="none"/></svg>`,
  // planning: a route drawing itself between three points
  plan: `<svg class="ic-plan" viewBox="0 0 18 18" ${A}><path class="live route" d="M3.5 13.5L9 5l5.5 6" stroke-dasharray="20"/>
    <circle cx="3.5" cy="13.5" r="1.4"/><circle cx="9" cy="5" r="1.4"/><circle cx="14.5" cy="11" r="1.4"/></svg>`,
  // writing files: a sheet rising out of a tray
  export: `<svg class="ic-export" viewBox="0 0 18 18" ${A}><path d="M3 10v5h12v-5"/>
    <path class="live sheet" d="M6.5 11V4.5h5V11"/></svg>`,
  // done: a plain tick
  done: `<svg viewBox="0 0 18 18" ${A}><path d="M4.5 9.5l3 3 6-6.5"/></svg>`,
  // a note: a small ring with a dot
  note: `<svg viewBox="0 0 18 18" ${A}><circle cx="9" cy="9" r="6"/><path d="M9 5.8v3.8"/><circle cx="9" cy="12.2" r=".6" fill="currentColor"/></svg>`,
};

document.querySelectorAll("[data-ic]").forEach((el) => { el.innerHTML = ICONS[el.dataset.ic] || ""; });

/* live counters: <span data-count-from="1180000" data-count-step="9000" data-count-fmt="m"> */
const fmt = (n, f) => f === "m" ? (n / 1e6).toFixed(2) + "M" : Math.round(n).toLocaleString("en-GB");
document.querySelectorAll("[data-count-from]").forEach((el) => {
  let n = +el.dataset.countFrom; const step = +el.dataset.countStep, max = +(el.dataset.countMax || Infinity);
  const bar = el.closest(".step") && el.closest(".step").querySelector(".bar i");
  const tick = () => { n = Math.min(max, n + step * (0.6 + Math.random() * 0.8)); el.textContent = fmt(n, el.dataset.countFmt);
    if (bar && el.dataset.countOf) bar.style.setProperty("--p", Math.min(100, 100 * n / +el.dataset.countOf).toFixed(1) + "%"); };
  tick(); if (!matchMedia("(prefers-reduced-motion: reduce)").matches) setInterval(tick, 900);
});
/* a clock: <span data-clock="372"> counts up from 372 s */
document.querySelectorAll("[data-clock]").forEach((el) => {
  let s = +el.dataset.clock;
  const show = () => { el.textContent = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`; };
  show(); setInterval(() => { s++; show(); }, 1000);
});
