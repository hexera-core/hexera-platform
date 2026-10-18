// Responsibility: Report which geometry the product accepts, as the backend describes it.
// Boundaries: advisory - the server validates every upload, so a failed capability request permits selection.

/* What geometry the product currently accepts, as the BACKEND describes it.
 *
 * The list used to be duplicated here, in the picker's accept attribute and in the rejection
 * copy, and it had already drifted from the server: .vtp was accepted server-side and named in
 * the server's own error text while this file refused it, so the VMTK engine's input format
 * could not be uploaded at all.
 *
 * This is ADVISORY. The server validates every upload, so when the capability request fails the
 * browser deliberately permits selection rather than inventing a client-side allowlist - a
 * degraded fetch must not become the security boundary.
 */
import { headers } from "../api/client.js";

let _cache = null;

/** The capability description, or null when it could not be fetched. */
export async function loadIntakeFormats() {
  if (_cache) return _cache;
  try {
    const r = await fetch("/api/v1/client-config", { headers: headers() });
    if (!r.ok) return null;
    const intake = (await r.json()).intake;
    if (!intake || !Array.isArray(intake.formats)) return null;
    _cache = intake;
    return _cache;
  } catch {
    return null;                       // offline / blocked: fall through to permissive
  }
}

/** Advisory accept attribute; empty string means "offer everything, let the server decide".
 *  It also offers the native CAD the server recognises (a SolidWorks part, a CATIA product...),
 *  so picking one explains how to export a STEP from it instead of the picker hiding the file
 *  with no reason given. Falls back to the accepted list when the server does not send one. */
export function acceptAttribute(intake) {
  if (!intake) return "";
  return intake.picker_accept || intake.accept || "";
}

/** True when the browser should let this filename through to the server. */
export function isOfferable(filename, intake) {
  if (!intake) return true;            // no capability data: never reject client-side
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return false;
  const suffix = filename.slice(dot).toLowerCase();
  return intake.formats.some((f) => f.suffixes.includes(suffix));
}

/** Human list for copy, derived from the same response - never a second hardcoded string.
 *  Split into CAD and meshes when the server says which is which (each format's `kind`), so a
 *  list of twenty formats still reads at a glance. */
export function supportedCopy(intake) {
  if (!intake) return "Upload a geometry file.";
  const list = (fs) => fs.map((f) => f.suffixes.join(" / ")).join(", ");
  const cad = intake.formats.filter((f) => f.kind === "cad");
  const mesh = intake.formats.filter((f) => f.kind !== "cad");
  const labels = cad.length && mesh.length
    ? `CAD ${list(cad)}; meshes ${list(mesh)} (a volume mesh is read as its surface)`
    : list(intake.formats);
  return `Accepted geometry formats: ${labels}. Compatibility with a particular mesh engine is `
       + `checked after upload.`;
}

/** The suffix a refusal talks about: the file's own, or .prt/.asm for a numbered Creo save
 *  (bracket.prt.3). The same rule as refusal_suffix in contracts/intake_formats.py. */
export function refusalSuffix(filename) {
  const name = String(filename || "").toLowerCase();
  const creo = name.match(/(\.(?:prt|asm))\.\d+$/);
  if (creo) return creo[1];
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot);
}

/** What the user reads when a file is not offerable. For native CAD it is the server's own
 *  sentence, word for word: which tool made the file and how to export a STEP from it. */
export function refusalCopy(filename, intake) {
  const native = intake && intake.native_cad;
  const said = native ? native[refusalSuffix(filename)] : "";
  return typeof said === "string" && said ? said : supportedCopy(intake);
}
