// Responsibility: The reading instruments on the delivered mesh - the inspect window's own
//                 controls, the size legend, and the plain words that say what is on screen.
// Owns: the overlay's markup, the dial's scale, every length printed to a reader, and the help.
// Boundaries: no vtk, no fetch, no DOM beyond the string it returns. viewer.js owns the vtk
//             boundary and the render loop; this owns what a reader is told.
// Collaborates with: viewer/viewer.js (mounts it and feeds it measurements), render/detail_window
//                    (the cut these controls ask for), css/viewer.css (.v-insp).

/* WHY THIS EXISTS, in the words of the person who could not read the old one.
 *
 * The cut into the real cells used to be one button called "Detail", built with
 * `className='v-dlbtn'` and appended to `.v-dl` - the DOWNLOADS bar. So the control that reads the
 * mesh was wearing a download button's clothes and standing in the download button's row, next to
 * ParaView. Asked what it did, the owner of this product said he could not tell, and that it
 * "has a download symbol next to it". It did: its own.
 *
 * Three things were wrong, and none of them was the cut.
 *
 *   1. IT LOOKED LIKE A DOWNLOAD. Same class, same bar, same shape as the file buttons.
 *   2. IT CUT WHERE THE CAMERA WAS, not where the reader was looking at. The ball's centre was
 *      `cam.getFocalPoint()` and its radius half the view's height, so to inspect a place you had
 *      to first aim the whole camera at it. Nobody aims a camera to point at something; they
 *      point at it.
 *   3. IT SAID NOTHING ABOUT SCALE. The drawn surface is merged to fit a browser - about sixteen
 *      delivered faces to one drawn one on the first customer plate - and nothing on screen said
 *      so, nor how big anything was. On a part whose passages are 100 to 300 um, a reader with no
 *      legend cannot tell a channel from a fillet.
 *
 * So: click the mesh to inspect it, a dial for how much, and a legend that always says how big
 * what you are looking at is and whether it is the real cells or the merged stand-in.
 */

/** Metres, printed the way the part is actually specified. A cold plate's passages are hundreds of
 *  microns and its envelope is hundreds of millimetres, so one unit cannot serve both and a
 *  reader given "0.000264 m" has been given arithmetic homework. */
export function fmtLen(m) {
  const v = Math.abs(Number(m));
  if (!(v > 0) || !isFinite(v)) return "-";
  if (v < 1e-6) return (v * 1e9).toPrecision(2) + " nm";
  if (v < 1e-3) return (v * 1e6).toPrecision(3) + " µm";
  if (v < 1) return (v * 1e3).toPrecision(3) + " mm";
  return v.toPrecision(3) + " m";
}

/** A size legend that is a round number, not a round pixel count.
 *
 *  A bar of exactly 120 px labelled "237 um" is a bar a reader cannot use: to judge a feature they
 *  must divide. So the LABEL is snapped to 1, 2 or 5 of a decade and the bar takes whatever width
 *  that costs, which is how every map and every micrograph does it. */
export function scaleBar(mPerPx, maxPx = 140) {
  const want = mPerPx * maxPx;
  if (!(want > 0) || !isFinite(want)) return { px: 0, label: "-", metres: 0 };
  const dec = Math.pow(10, Math.floor(Math.log10(want)));
  const mult = [5, 2, 1].find((k) => k * dec <= want) || 1;
  const metres = mult * dec;
  return { px: Math.round(metres / mPerPx), label: fmtLen(metres), metres };
}

/** The dial's radius, geometric from a few of the finest cells up to the whole part.
 *
 *  GEOMETRIC AND NOT LINEAR because the useful range spans four decades: the plate's finest cell
 *  is 8.3 um and its envelope 170 mm, so a linear dial spends ninety of its hundred steps on
 *  radii that return the same whole-part answer and cannot be nudged onto one channel at all.
 *  The floor is the old button's floor - four of the finest cells - so arming the dial at zero
 *  gives what pressing Detail gave. */
export function dialRadius(v, { fine, diag }) {
  const lo = Math.max(Number(fine) || 0, 1e-12);
  const hi = Math.max(Number(diag) || 0, lo * 1.0001) / 2;
  const t = Math.min(1, Math.max(0, Number(v) / 100));
  return lo * Math.pow(hi / lo, t);
}

/** Where a radius sits on that dial, so arming inspect can start from what the camera already
 *  frames instead of from an arbitrary step. */
export function radiusDial(r, { fine, diag }) {
  const lo = Math.max(Number(fine) || 0, 1e-12);
  const hi = Math.max(Number(diag) || 0, lo * 1.0001) / 2;
  const t = Math.log(Math.max(r, lo) / lo) / Math.log(hi / lo);
  return Math.round(100 * Math.min(1, Math.max(0, t)));
}

/** How many delivered faces stand behind one drawn one, or 1 when nothing was merged. The viewer
 *  carries both counts in `reduced` (a budget clustered them) or in `exact` (coplanar ones were
 *  merged losslessly); this is the number a reader needs to hear, not the two.
 *
 *  POLYGONS BEFORE TRIANGLES where both are stated. The question behind this number is "how many
 *  cells is the thing under my cursor", and the thing under the cursor is a polygon - a quad
 *  drawn as two triangles is still one face of the drawn surface, so counting triangles halves
 *  the answer. The lossy block states only triangles, so that is what it gets measured on. */
export function mergeFactor(prov) {
  if (!prov) return 1;
  const a = Number(prov.delivered_faces) || 0;
  const b = Number(prov.drawn_polygons) || Number(prov.drawn_triangles) || 0;
  if (!(a > 0) || !(b > 0)) return 1;
  return Math.max(1, a / b);
}

/** Which of the two drawing routes a payload took, and its provenance block.
 *
 *  THE TWO ARE NOT THE SAME CLAIM AND MUST NOT SHARE A SENTENCE. `reduced` means faces were
 *  clustered until the surface fit a triangle budget, so the drawn surface APPROXIMATES the mesh -
 *  measured on the first customer cold plate, it draws 98.65% of the coolant's area. `exact` means
 *  coplanar axis-aligned faces were merged into the rectangles they already tiled, so the drawn
 *  surface IS the mesh: same area to 1e-12, and what a polygon loses is only the subdivision
 *  inside it. Telling a reader the second is a stand-in is as wrong as telling them the first is
 *  the mesh.
 *
 *  The difference is NOT dramatic to look at - the two differ in under 1% of canvas pixels at the
 *  same camera - so this wording is about being accurate, not about explaining something obvious
 *  on screen. */
export function drawingOf(surf) {
  const s = surf || {};
  const exact = !!(s.exact && Number(s.exact.drawn_polygons));
  const block = exact ? s.exact : (s.reduced || null);
  return { exact, block, factor: mergeFactor(block) };
}

/** The one line that says what is on screen: how big a pixel is, and whether the surface under
 *  the cursor is the mesh or a stand-in for it.
 *
 *  SAID, NOT IMPLIED - the same rule the cut's own label follows. A merged surface that does not
 *  announce itself reads as the mesh, and then every judgement a reader makes about cell size,
 *  channel width or wall thickness is a judgement about an average they were never shown. */
export function odometerText({ mPerPx, merged = 1, real = false, exact = false }) {
  const px = (mPerPx > 0 && isFinite(mPerPx)) ? "1 px = " + fmtLen(mPerPx) : "";
  // AN EXACT SURFACE MUST NOT SAY "STAND-IN", because it is not one: the shape under the cursor
  // is the mesh's own to the last nanometre. What it does not show is the subdivision inside a
  // polygon, so that is what it says, and it says how much of it.
  const what = real
    ? "real cells"
    : exact
      ? (merged > 1.5
          ? "the mesh exactly · one polygon spans about " + Math.round(merged) + " cells"
          : "the mesh exactly, one polygon per cell")
      : (merged > 1.5
          ? "merged surface, about " + Math.round(merged) + " faces to one drawn"
          : "the delivered faces");
  return px ? px + " · " + what : what;
}

/** What the cut just returned, in a sentence. Truncation is the case that matters: a reader handed
 *  150,000 of 404,725 faces with no word about it reads the gap as mesh that is not there. */
export function cutText(got) {
  if (!got) return "";
  const ret = Number(got.faces_returned) || 0;
  const have = Number(got.faces_in_range) || 0;
  if (!ret) return "no cells in that window";
  if (got.truncated && have > ret) {
    return ret.toLocaleString() + " of " + have.toLocaleString()
      + " faces here · narrow the window for all of them";
  }
  return ret.toLocaleString() + " real cells, unmerged";
}

/** The help, in the order a reader meets the thing. No jargon: a reader who knew what a boundary
 *  index was would not need this panel. */
/** What the first help row says on an EXACT surface, where the row below it would be false.
 *
 *  "A stand-in" is the right word for a budget-clustered surface and the wrong one here. On this
 *  route the shape on screen is the mesh's own - the coplanar faces that were merged tiled the
 *  rectangle they became, so nothing moved - and the only thing a reader cannot see is where one
 *  polygon is subdivided into cells. Saying "stand-in" would send them to Inspect to check a
 *  shape that is already exact. */
export const EXACT_HELP_ROW = [
  "What the mesh on screen is",
  "The mesh itself. Faces that lay flat in the same plane were merged into the larger rectangles "
  + "they already tiled, which moves nothing - the whole part is 19,599,615 delivered faces drawn "
  + "as 1,297,428 polygons covering the same area to eleven decimal places. So every shape here "
  + "is real: channel widths, wall thicknesses and corners are the mesher's own. What a polygon "
  + "does not show is the cell boundaries inside it, and the legend at the bottom left says how "
  + "many cells that is. Inspect and Follow are how you see those.",
];

export const INSPECT_HELP = [
  ["What the mesh on screen is",
   "A stand-in. The real mesh is far too big to send to a browser, so neighbouring faces are "
   + "merged until it fits. The shape is right; the individual cells you see are not the "
   + "mesh's cells. The legend at the bottom left says so whenever that is true."],
  ["What Inspect does",
   "Turn it on and click anywhere on the mesh. It fetches the REAL cells in a ball around the "
   + "spot you clicked and draws them in place of the stand-in. Those are the actual cells the "
   + "mesher wrote, one drawn polygon per face, so what you measure on them is the mesh."],
  ["Follow",
   "The same real cells, but wherever you look instead of wherever you clicked. Turn it on and "
   + "zoom: while a cell is too small to see, it keeps the stand-in and tells you how many pixels "
   + "a cell is worth; once they are worth looking at it reads the cells in front of you and "
   + "draws them, and re-reads whenever you stop moving. It sizes its own window - if what you "
   + "are looking at holds more cells than can be drawn it narrows until every cell in the "
   + "window comes back, rather than handing you a sample of them. The whole boundary is "
   + "19,599,615 faces and cannot be drawn at once on this machine; this is how you reach all "
   + "of it."],
  ["The dial",
   "How big that ball is. All the way left is a few of the finest cells; all the way right is "
   + "the whole part. The number beside it is the real size, and the count tells you how many "
   + "cells came back."],
  ["Shell",
   "Draws the metal around the coolant, see-through, so you can see the channels running inside "
   + "it. It starts off: a cooled plate is a block with passages cut through it, and drawn solid "
   + "that block is the only thing you can see. Slide it right to put the metal back as a ghost "
   + "and get the channels in context."],
  ["Clip",
   "Cuts the view with a flat plane you can slide through the part. Nothing is removed from the "
   + "mesh; it is only hidden on one side of the plane so you can see in."],
  ["Worst",
   "Flies the camera to the single worst face the mesh has on the metric you picked, and opens "
   + "the inspect window on it. A number like 79 degrees becomes a place you can look at."],
  ["Fly",
   "Walks the camera along the part's long axis, so a channel network can be followed from inlet "
   + "to outlet instead of orbited."],
];

/** The overlay. It sits ON the canvas, not in the panel beside it, because every control here
 *  changes what the canvas shows and a control that lives elsewhere is a control a reader has to
 *  go and find. */
export function inspectorHtml(job, { xray = false, clip = false, fly = false, worst = false,
                                     exact = false } = {}) {
  const id = (k) => `v-insp-${k}-${job}`;
  // the first row is the one that differs between the two drawing routes; the rest describe the
  // controls, which behave identically either way
  const help = exact ? [EXACT_HELP_ROW, ...INSPECT_HELP.slice(1)] : INSPECT_HELP;
  return `<div class="v-insp" id="${id('box')}" data-armed="0">
    <div class="v-insp-main">
      <button class="v-insp-act" id="${id('arm')}" type="button" aria-pressed="false"
              title="Turn on, then click the mesh to read the real cells there">
        <span class="v-insp-dot"></span><b>Inspect</b></button>
      <button class="v-insp-b" id="${id('follow')}" type="button" aria-pressed="false"
              title="Keep reading the real cells wherever you look, instead of clicking one spot">
        Follow</button>
      <button class="v-insp-b" id="${id('stream')}" type="button" aria-pressed="false"
              title="Fill the whole view with real cells, a tile at a time, up to what a screen can show">
        Stream</button>
      <label class="v-insp-dial" title="how big a ball of real cells to read">
        <input type="range" id="${id('dial')}" min="0" max="100" value="38"
               aria-label="inspect window size">
        <span class="v-insp-r" id="${id('r')}">-</span></label>
      <span class="v-insp-read" id="${id('read')}">click the mesh</span>
      <span class="v-insp-sp"></span>
      ${worst ? `<button class="v-insp-b" id="${id('worst')}" type="button"
            title="fly to the worst face on the metric in view and inspect it">Worst</button>` : ""}
      ${fly ? `<button class="v-insp-b" id="${id('fly')}" type="button"
            title="walk the camera along the part's long axis">Fly</button>` : ""}
      <button class="v-insp-b v-insp-q" id="${id('help')}" type="button"
              aria-expanded="false" title="what these controls do">?</button>
    </div>
    <div class="v-insp-more">
      ${xray ? `<label class="v-insp-dial"
             title="draw the metal around the coolant, see-through, so the channels show inside it">
        <span>Shell</span>
        <input type="range" id="${id('xray')}" min="0" max="100" value="0"
               aria-label="how solid to draw the surrounding metal">
        <span class="v-insp-r" id="${id('xrayr')}">off</span></label>` : ""}
      ${clip ? `<span class="v-insp-clip" title="hide everything on one side of a flat plane">
        <span>Clip</span>
        <button class="v-insp-c" data-ax="" aria-pressed="true">off</button>
        <button class="v-insp-c" data-ax="x">X</button>
        <button class="v-insp-c" data-ax="y">Y</button>
        <button class="v-insp-c" data-ax="z">Z</button>
        <input type="range" id="${id('clip')}" min="0" max="100" value="50" disabled
               aria-label="clip plane position">
        <button class="v-insp-c" id="${id('flip')}" title="swap which side is hidden">flip</button>
      </span>` : ""}
    </div>
    <div class="v-insp-help" id="${id('helpbox')}" hidden>
      ${help.map(([h, p]) => `<div class="v-insp-hrow"><b>${h}</b><p>${p}</p></div>`).join("")}
    </div>
  </div>
  <div class="v-legend-scale" id="v-scale-${job}">
    <div class="v-scale-bar"><i id="v-scale-i-${job}"></i></div>
    <span class="v-scale-l" id="v-scale-l-${job}">-</span>
    <span class="v-scale-o" id="v-scale-o-${job}"></span>
  </div>`;
}

export default { fmtLen, scaleBar, dialRadius, radiusDial, mergeFactor, drawingOf, odometerText,
                 cutText, inspectorHtml, INSPECT_HELP, EXACT_HELP_ROW };
