// Responsibility: The floating toolbar every 3D view shares - its buttons, the one small overlay each
// opens, the axes in the corner, the colours the part is drawn in, and the picture a snapshot saves.
// Boundaries: it draws page chrome and reads the camera; what each tool DOES to the scene is the
// viewer's own, handed in as callbacks. It fetches nothing and knows nothing about meshes.

/* THE TOOLBAR. One compact bar floats at the top of the canvas: View, Section, Parts, Measure,
   Snapshot (a viewer lists only the tools it has). Each button opens one small overlay under it;
   only one is open at a time. Esc closes it and gives focus back to its button; so does a click
   outside it - except a drag on the part, which only turns the view, and a click on the part while
   a tool that works by clicking the part (Measure) is open. */

const S = 'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"';
export const ICONS = {
  view: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M8 1.8l5.5 3.1v6.2L8 14.2l-5.5-3.1V4.9z"/><path d="M2.5 4.9L8 8l5.5-3.1M8 8v6.2"/></svg>`,
  section: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M3 11l6-8 4 3-6 8z" opacity=".5"/><path d="M1.5 9.5l13-3"/></svg>`,
  parts: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M8 2.5l6 3-6 3-6-3z"/><path d="M2 8.5l6 3 6-3"/><path d="M2 11.2l6 3 6-3" opacity=".6"/></svg>`,
  measure: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M2.5 10.5l8-8 3 3-8 8z"/><path d="M5 8l1.3 1.3M7 6l1.3 1.3M9 4l1.3 1.3"/></svg>`,
  snapshot: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M2 5.5h2.6L6 3.8h4l1.4 1.7H14v7H2z"/><circle cx="8" cy="9" r="2.2"/></svg>`,
  caret: `<svg class="vt-caret" viewBox="0 0 8 8" ${S} aria-hidden="true"><path d="M1.5 3l2.5 2.5L6.5 3"/></svg>`,
  search: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2L13.5 13.5"/></svg>`,
  eyeOn: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>`,
  eyeOff: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" opacity=".45"/><path d="M3 13L13 3"/></svg>`,
  close: `<svg viewBox="0 0 16 16" ${S} aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>`,
};

const LABELS = { view: "View", section: "Section", parts: "Parts", measure: "Measure", snapshot: "Snapshot" };
// the tools that work by clicking the part stay open while the user clicks it
const CLICKS_THE_PART = new Set(["measure"]);

/** The toolbar and its overlays. `tools` is [{key, body}] in order; `prefix` makes the ids unique
 *  per viewer. Measure sits after a hairline: it is a tool, the others are ways of looking. */
export function toolbarHtml(prefix, tools) {
  const buttons = tools.map(({ key }) => {
    const sep = key === "measure" ? '<span class="vt-sep" aria-hidden="true"></span>' : "";
    return `${sep}<button type="button" class="vt-btn" data-tool="${key}" id="${prefix}-vt-${key}" `
      + `aria-haspopup="dialog" aria-expanded="false" aria-controls="${prefix}-ov-${key}">`
      + `${ICONS[key] || ""}<span>${LABELS[key] || key}</span>${key === "measure" ? "" : ICONS.caret}</button>`;
  }).join("");
  const overlays = tools.map(({ key, body }) =>
    `<div class="v-ov v-ov-${key}" id="${prefix}-ov-${key}" data-tool="${key}" role="dialog" `
    + `aria-label="${LABELS[key] || key}" hidden>${body}</div>`).join("");
  return `<div class="vt" role="toolbar" aria-label="View tools">${buttons}</div>${overlays}`;
}

/** Wire the toolbar inside `host` (the canvas). `onOpen(key)` / `onClose(key)` let the viewer arm
 *  or disarm a tool. Returns {open, close, current}. */
export function bindToolbar(host, { onOpen, onClose } = {}) {
  const bar = host.querySelector(".vt");
  if (!bar) return { open() {}, close() {}, current: () => null };
  let current = null;
  const btn = (k) => bar.querySelector(`.vt-btn[data-tool="${k}"]`);
  const pane = (k) => host.querySelector(`.v-ov[data-tool="${k}"]`);

  function place(k) {
    // the overlay hangs under its button, kept inside the canvas
    const b = btn(k), o = pane(k);
    if (!b || !o) return;
    const hr = host.getBoundingClientRect(), br = b.getBoundingClientRect();
    const w = o.offsetWidth || 300;
    const left = Math.max(8, Math.min(br.left - hr.left, hr.width - w - 8));
    o.style.left = left + "px";
    o.style.top = (br.bottom - hr.top + 8) + "px";
  }
  function close(focusBack) {
    if (!current) return;
    const k = current;
    current = null;
    pane(k).hidden = true;
    btn(k).setAttribute("aria-expanded", "false");
    if (onClose) onClose(k);
    if (focusBack) btn(k).focus();
  }
  function open(k, byKey) {
    if (current === k) return;
    close(false);
    if (!pane(k)) return;
    current = k;
    pane(k).hidden = false;
    btn(k).setAttribute("aria-expanded", "true");
    place(k);
    if (onOpen) onOpen(k, !!byKey);
  }
  bar.addEventListener("click", (e) => {
    const b = e.target.closest(".vt-btn");
    if (!b) return;
    const k = b.dataset.tool;
    // a button pressed from the keyboard has no pointer behind it (detail 0)
    if (current === k) close(false); else open(k, e.detail === 0);
  });

  // Esc closes; a click outside closes - a drag does not, and neither does a click on the part
  // while a tool that is used by clicking the part is open
  let down = null;
  const onKey = (e) => {
    if (!document.body.contains(host)) { document.removeEventListener("keydown", onKey); return; }
    if (e.key === "Escape" && current) { e.preventDefault(); close(true); }
  };
  document.addEventListener("keydown", onKey);
  const onDown = (e) => { down = [e.clientX, e.clientY]; };
  const onUp = (e) => {
    if (!document.body.contains(host)) {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("pointerup", onUp, true);
      return;
    }
    if (!current || !down) return;
    const moved = Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6;
    down = null;
    if (moved) return;
    const t = e.target;
    if (t && t.closest && (t.closest(".vt") || t.closest(".v-ov") || t.closest(".v-picked"))) return;
    if (CLICKS_THE_PART.has(current) && host.contains(t)) return;
    close(false);
  };
  document.addEventListener("pointerdown", onDown, true);
  document.addEventListener("pointerup", onUp, true);
  new ResizeObserver(() => { if (current) place(current); }).observe(host);
  return { open: (k) => open(k, false), close: () => close(false), current: () => current };
}

/* SMALL CONTROLS used inside the overlays */

/** A row: a plain label on the left, a control on the right. */
export const optRow = (label, control, { cls = "", hidden = false } = {}) =>
  `<div class="vo-opt${cls ? " " + cls : ""}"${hidden ? " hidden" : ""}><span class="vo-k">${label}</span>${control}</div>`;

/** A small segmented choice. `opts` is [[value, label]]. */
export function segHtml(name, opts, value, label) {
  return `<div class="vo-seg" role="group" aria-label="${label || name}" data-seg="${name}">`
    + opts.map(([v, l]) => `<button type="button" data-v="${v}" aria-pressed="${String(v === value)}">${l}</button>`).join("")
    + "</div>";
}
/** Wire every segmented choice named `name` under `root`: `fn(value)` on a press. */
export function bindSeg(root, name, fn) {
  root.querySelectorAll(`.vo-seg[data-seg="${name}"]`).forEach((g) => {
    g.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (!b || b.disabled) return;
      setSeg(root, name, b.dataset.v);
      fn(b.dataset.v);
    });
  });
}
export function setSeg(root, name, value) {
  root.querySelectorAll(`.vo-seg[data-seg="${name}"] button[data-v]`).forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.v === String(value)));
  });
}

/* THE AXES - a small X, Y, Z in the corner of the canvas, turning with the part, so the user always
   knows which way its coordinates run. An axis pointing away from the viewer is dimmed. */
export function axesHtml(cls = "v-axes") {
  return `<svg class="${cls}" viewBox="0 0 84 84" aria-hidden="true">`
    + ["x", "y", "z"].map((k) => `<line class="ax ax-${k}" x1="42" y1="42" x2="42" y2="42"/>`
      + `<text class="ax-l ax-${k}" x="42" y="42">${k.toUpperCase()}</text>`).join("")
    + "</svg>";
}
/** Draw the axes from the camera; returns each axis on the screen as [right, up, out]. */
export function drawAxes(svg, cam) {
  const out = {};
  if (!svg || !cam) return out;
  const dir = cam.getDirectionOfProjection(), up = cam.getViewUp();
  const right = [dir[1] * up[2] - dir[2] * up[1], dir[2] * up[0] - dir[0] * up[2], dir[0] * up[1] - dir[1] * up[0]];
  const rl = Math.hypot(right[0], right[1], right[2]) || 1;
  right[0] /= rl; right[1] /= rl; right[2] /= rl;
  const upp = [right[1] * dir[2] - right[2] * dir[1], right[2] * dir[0] - right[0] * dir[2], right[0] * dir[1] - right[1] * dir[0]];
  const C = 42, L = 28;
  ["x", "y", "z"].forEach((k, i) => {
    const sx = right[i], sy = upp[i], sz = -dir[i];
    out[k] = [sx, sy, sz];
    const line = svg.querySelector("line.ax-" + k), label = svg.querySelector("text.ax-" + k);
    if (!line || !label) return;
    line.setAttribute("x2", (C + L * sx).toFixed(1)); line.setAttribute("y2", (C - L * sy).toFixed(1));
    label.setAttribute("x", (C + (L + 9) * sx).toFixed(1)); label.setAttribute("y", (C - (L + 9) * sy).toFixed(1));
    line.classList.toggle("back", sz < 0); label.classList.toggle("back", sz < 0);
  });
  return out;
}

/* THE PART'S COLOURS come from the theme (tokens.css), read when a view opens, so a new theme
   recolours the 3D part with the rest of the page. The values passed here are only the fallback
   for a page that does not load the tokens. */
export function parseColour(s) {
  s = String(s || "").trim();
  let m = /^#([0-9a-f]{6})$/i.exec(s);
  if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255);
  m = /^#([0-9a-f]{3})$/i.exec(s);
  if (m) return [0, 1, 2].map((i) => parseInt(m[1][i] + m[1][i], 16) / 255);
  m = /^rgba?\(([^)]+)\)$/i.exec(s);
  if (m) { const v = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); if (v.length >= 3 && v.slice(0, 3).every(isFinite)) return v.slice(0, 3).map((x) => x / 255); }
  return null;
}
export function themeRgb(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return parseColour(v) || fallback;
  } catch { return fallback; }
}
/** A colour as the CSS a swatch is painted with. */
export const cssRgb = (c) => `rgb(${c.map((v) => Math.round(v * 255)).join(",")})`;

/* THE SNAPSHOT. The render window draws one frame at the chosen size (1x, 2x or 4x the canvas)
   onto the chosen background - the scene's own, transparent, or white - and the colour bar can be
   drawn in beside the part. Nothing on the page is changed by it. */
export async function snapshotPng({ ren, rw, apiRW, background, scale, sceneBg, bar }) {
  const old = ren.getBackground().slice();
  const white = background === "white", clear = background === "transparent";
  if (white) ren.setBackground(1, 1, 1, 1);
  else if (clear) ren.setBackground(sceneBg[0], sceneBg[1], sceneBg[2], 0);
  let url;
  try {
    const p = apiRW.captureNextImage("image/png", { scale: scale || 1 });
    rw.render();
    url = await p;
  } finally {
    ren.setBackground(...(old.length === 3 ? [...old, 1] : old));
    rw.render();
  }
  const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error("snapshot image")); i.src = url; });
  const cv = document.createElement("canvas");
  cv.width = img.naturalWidth; cv.height = img.naturalHeight;
  const g = cv.getContext("2d");
  g.drawImage(img, 0, 0);
  if (bar) drawBar(g, cv.width, cv.height, (scale || 1) * Math.min(window.devicePixelRatio || 1, 2), bar, white);
  const blob = await new Promise((res) => cv.toBlob(res, "image/png"));
  return { blob, width: cv.width, height: cv.height, canvas: cv };
}

/* the colour bar on the picture: the metric's name, an upright ramp with the limit as a tick, and
   its top, limit and bottom values - the same stops the bar on the canvas shows */
function drawBar(g, W, H, k, bar, onWhite) {
  const ink = onWhite ? themeCss("--surface-2", "#1c1c1a") : themeCss("--text", "#ede9e0");
  const faint = themeCss("--text-faint", "#8a8884");
  const font = themeCss("--sans", "sans-serif");
  const bw = 12 * k, bh = Math.min(220 * k, H * 0.5), x = W - 28 * k - bw, y = H - 28 * k - bh;
  const grad = g.createLinearGradient(0, y + bh, 0, y);
  bar.stops.forEach(([f, c]) => grad.addColorStop(Math.max(0, Math.min(1, f)), `rgb(${c.join(",")})`));
  g.fillStyle = grad;
  g.fillRect(x, y, bw, bh);
  g.textAlign = "right"; g.textBaseline = "middle";
  bar.ticks.forEach(([f, text, strong]) => {
    const ty = y + bh * (1 - f);
    g.fillStyle = strong ? ink : faint;
    g.font = `${strong ? 600 : 400} ${11 * k}px ${font}`;
    g.fillText(text, x - 8 * k, ty);
    if (strong) g.fillRect(x - 3 * k, ty - k, bw + 6 * k, 2 * k);
  });
  g.textAlign = "right"; g.textBaseline = "alphabetic";
  g.fillStyle = ink; g.font = `600 ${12 * k}px ${font}`;
  g.fillText(bar.label, x + bw, y - 10 * k);
}
function themeCss(name, fallback) {
  try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; }
  catch { return fallback; }
}

/** Hand a picture to the browser as a download. */
export function savePng(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
