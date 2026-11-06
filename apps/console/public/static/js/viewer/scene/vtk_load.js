// Responsibility: Load vtk.js once for the whole page, and decode the base64 buffers every scene
// is handed - point coordinates, connectivity, and per-cell bytes.
// Owns: the single <script> tag for /static/vendor/vtk.js and the one promise that stands for it.
// Boundaries: it builds no scene and reads no DOM but the script tag it appends; it knows nothing
// about a part, a mesh or a job. It is the vendor boundary and nothing else.

/* ONE LOADER FOR EVERY SCENE.
 *
 * The geometry stage and the delivered-mesh viewer each carried their own copy of this, each with
 * its own module-local promise - two independent answers to "has vtk.js been asked for yet?" over
 * one shared global. The progressive mesh and the review view already borrowed the geometry
 * stage's copy, which is the clearest sign the loader never belonged to a scene in the first
 * place. One module, one promise, one script tag.
 *
 * THE LAZINESS IS THE CONTRACT. Nothing here runs on import: vtk.js is fetched by the first
 * `loadVtk()` call and by nothing else, so a caller that defers that call - the mesh viewer waits
 * for its panel to intersect the viewport - still defers the whole vendor bundle.
 */

let _vtkP = null;

/** Resolves once `window.vtk` is there. The first call starts the fetch; every later one waits on
 *  that same fetch, whoever made it. */
export function loadVtk() {
  if (window.vtk) return Promise.resolve();
  if (!_vtkP) _vtkP = new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = "/static/vendor/vtk.js";
    s.onload = res; s.onerror = () => rej(new Error("vtk.js failed to load"));
    document.head.appendChild(s); });
  return _vtkP;
}

/** The raw bytes behind a base64 field - a per-cell lookup is handed over as-is. */
export function b64u8(b) { const bin = atob(b), n = bin.length, u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = bin.charCodeAt(i); return u; }
/** Point coordinates, normals, and the per-cell float fields. */
export function b64f32(b) { return new Float32Array(b64u8(b).buffer); }
/** Connectivity: vtk's own cell arrays. */
export function b64u32(b) { return new Uint32Array(b64u8(b).buffer); }
