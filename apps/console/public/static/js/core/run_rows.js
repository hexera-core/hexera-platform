// Responsibility: Say which of the run box's four rows a backend event belongs to - THE ONE TABLE, for every route.
// Owns: the four rows (their titles, what each says while it runs, what each says when a route has nothing for it),
// which backend stage feeds which row, and the one rule that splits the builder's work into designing and creating.
// Boundaries: pure - no DOM, no renderer. The renderer (render/stage.js) and the live line (render/run_progress.js)
// both read their rows from here, so the box looks the same for FEA, CFD, a thermal model, a shell and any route after.

/* THE FOUR ROWS, in the order a run goes through them. Every route shows all four: a route that
 * skips one still shows it, with its `none` words, so the box never changes shape. */
export const ROWS = [
  { key: "engine", title: "Choosing the engine", doing: "Choosing the engine…",
    none: "The engine was set before the run started." },
  { key: "design", title: "Designing the mesh", doing: "Designing the mesh…",
    none: "Nothing to design here: the engine sets its sizes itself.",
    quiet: "The engine set its sizes as it built; its steps are under this row." },
  { key: "build", title: "Creating the mesh", doing: "Creating the mesh…",
    none: "No mesh was built." },
  { key: "review", title: "Reviewing the mesh", doing: "Reviewing the mesh…",
    none: "" },
];
export const ROW_KEYS = ROWS.map((r) => r.key);

/* WHICH ROW EACH BACKEND STAGE FEEDS. A new route that publishes under a stage of its own adds one
 * line here - nothing else in the box changes. A stage not listed joins the row the run is in. */
export const STAGE_ROW = {
  intake: "engine", engine_select: "engine", repair: "engine", geometry_admission: "engine",
  builder: "build", executor: "review", classifier: "review", reviewer: "review", outcome: "review",
};

/* THE BUILDER DESIGNS, THEN BUILDS. Everything the builder says in an attempt before its mesher starts
 * - measuring the part, planning, the decisions it says out loud, its plan - is the design; from the
 * moment the mesher starts (or the machine it runs on is said) it is creating the mesh. The design's
 * receipt (contracts/mesh_design) is the design whenever it arrives. */
export const DESIGNS_IN = "builder";

/** Whether this builder event starts the build: the mesher starting, or the machine it runs on. */
export function startsBuild(ev) {
  return !!ev && (ev.type === "meshing" || (ev.type === "narrate" && ev.kind === "machine"));
}

/** The row an event of `stage` goes to. `building` is whether this attempt's mesher has started;
 *  `fallback` is the row the run is in (for a stage this table does not know). Pure. */
export function rowFor(stage, ev, building, fallback = "engine") {
  const row = STAGE_ROW[stage];
  if (!row) return fallback || "engine";
  if (stage === DESIGNS_IN) {
    if (ev && ev.type === "receipt") return "design";
    if (startsBuild(ev)) return "build";
    return building ? "build" : "design";
  }
  return row;
}

/** A row's definition by key. */
export function rowDef(key) { return ROWS.find((r) => r.key === key) || ROWS[0]; }

/** What the engine row says for the engine sentence the backend sends: the row is already called
 *  "Choosing the engine", so "Mesh engine: Gmsh" reads "Gmsh". Anything else is shown as sent. */
export function engineWords(text) {
  const t = String(text || "").trim();
  return t.replace(/^Mesh engine:\s*/, "");
}
