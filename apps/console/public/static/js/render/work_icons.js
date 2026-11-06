// Responsibility: Draw the small animated status icon for each kind of work on a live line - reading,
// measuring, planning, refining, layering, filling, checking, writing files, and the machine.
// Boundaries: markup only, from a closed table; the motion lives in css/chat.css (and stops under
// reduced motion in css/a11y.css). It knows nothing of runs or checks.

const A = 'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"';
const hex = (cx, cy, r = 3) => [[r, 0], [r / 2, r * 0.866], [-r / 2, r * 0.866], [-r, 0], [-r / 2, -r * 0.866], [r / 2, -r * 0.866]]
  .map(([x, y]) => `${(cx + x).toFixed(1)},${(cy + y).toFixed(1)}`).join(" ");

/* one 18 px drawing per kind; the part marked `wi-live` is the one that moves, in the accent */
const ICONS = {
  // a page, a scan line walking down it
  read: `<path d="M4 2.5h6.5L14 6v9.5H4z"/><path d="M10.5 2.5V6H14"/><path class="wi-live wi-scan" d="M6 6.5h6"/>`,
  // a caliper, one jaw moving
  measure: `<path d="M2.5 4h13"/><path d="M4 4v10"/><path d="M13 4v1.6M15 4v1.6"/><path class="wi-live wi-jaw" d="M8.5 4v7"/>`,
  // a route drawing itself between three points
  plan: `<path class="wi-live wi-route" d="M3.5 13.5L9 5l5.5 6" stroke-dasharray="20"/>`
    + `<circle cx="3.5" cy="13.5" r="1.4"/><circle cx="9" cy="5" r="1.4"/><circle cx="14.5" cy="11" r="1.4"/>`,
  // a hexagon that splits into a finer one
  refine: `<polygon points="${hex(9, 9, 6.5)}"/><polygon class="wi-live wi-inner" points="${hex(9, 9, 6.5)}"/>`,
  // thin layers growing off a wall
  layer: `<path d="M2.5 15h13" stroke-width="2"/><path class="wi-live wi-l" d="M3 12.3h12"/>`
    + `<path class="wi-live wi-l wi-l2" d="M3 9.6h12"/><path class="wi-live wi-l wi-l3" d="M3 6.9h12"/>`,
  // hex cells lighting up in turn
  fill: `<g stroke-width="1.1"><polygon class="wi-c" points="${hex(5.2, 5.5)}"/><polygon class="wi-c wi-c2" points="${hex(9.7, 8.1)}"/>`
    + `<polygon class="wi-c wi-c3" points="${hex(5.2, 10.7)}"/><polygon class="wi-c wi-c4" points="${hex(14.2, 5.5)}"/></g>`,
  // a gauge needle settling
  check: `<path d="M3.5 13a5.5 5.5 0 0 1 11 0"/><path class="wi-live wi-needle" d="M9 12.5V7.4"/>`
    + `<circle cx="9" cy="12.6" r="1" fill="currentColor" stroke="none"/>`,
  // a sheet rising out of a tray
  export: `<path d="M3 10v5h12v-5"/><path class="wi-live wi-sheet" d="M6.5 11V4.5h5V11"/>`,
};
// the machine is work on a remote computer: drawn as the fill, the work it is doing
ICONS.machine = ICONS.fill;

/** The icon's markup for a kind of work, or "" for a kind with none (the plain dot stays). */
export function workIcon(kind) {
  const body = ICONS[kind];
  return body ? `<svg class="wi wi-${kind}" viewBox="0 0 18 18" ${A} aria-hidden="true" focusable="false">${body}</svg>` : "";
}

/** The kinds that have an icon. */
export const WORK_KINDS = Object.freeze(Object.keys(ICONS));
