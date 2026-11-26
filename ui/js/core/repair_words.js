// Responsibility: Say everything the customer reads about a CAD repair - the progress lane, the result's numbers,
//                 the per-problem trace and the pins' legend - in plain words, from the typed repair result and the
//                 typed `repair_progress` events.
// Owns: the plain name of every problem type, the three per-problem results, the choice's three options, and the
//       one guard that keeps internal vocabulary off the page.
// Boundaries: pure - no DOM, no network. The renderers (render/stage.js, render/repair.js, viewer/repair_view.js)
//             draw what this module says; none of them writes a repair sentence of its own.

/* THE LANGUAGE RULE (CRS v5 beta spec, owner-approved 2026-11-26). The customer never reads how the repair works
 * inside: no search strategy names, no internal step names, no operation or class identifiers and no exception
 * text. Every sentence here is built from TYPED fields (a problem type, an index, a count, a status). A free-text
 * field the backend sends (`text`, `reason_text`) is shown only when `isPlain` passes it, and is replaced by this
 * module's own words when it does not - so a leak upstream degrades to a plainer sentence, never to jargon. */

/** Plain name for every problem type (the spec's table). A type this build has not heard of is "unknown". */
export const PROBLEM_WORDS = {
  close_faces: "thin gap",
  overlap_self_intersection: "overlapping parts",
  surface_crossing: "intersecting surfaces",
  facet_crossing_local: "folded surface",
  low_quality: "tiny or sliver feature",
  open_shell_volume: "hole or open surface",
  invalid_brep_tolerance: "damaged geometry",
  closed_periodic_face: "seam problem",
  unknown: "problem we couldn't identify",
};

/** The plural of each plain name, for counts ("3 thin gaps"). */
const PLURAL = {
  "thin gap": "thin gaps",
  "overlapping parts": "overlapping parts",
  "intersecting surfaces": "intersecting surfaces",
  "folded surface": "folded surfaces",
  "tiny or sliver feature": "tiny or sliver features",
  "hole or open surface": "holes or open surfaces",
  "damaged geometry": "areas of damaged geometry",
  "seam problem": "seam problems",
  "problem we couldn't identify": "problems we couldn't identify",
};

/** The plain name of a problem type: always one of the table's, never the key itself. */
export function problemName(type) {
  return PROBLEM_WORDS[String(type || "")] || PROBLEM_WORDS.unknown;
}

/** "a thin gap", "an area of damaged geometry" - the name with its article, for a sentence. */
export function aProblem(type) {
  const n = problemName(type);
  if (n === "overlapping parts" || n === "intersecting surfaces") return n;
  if (n === "damaged geometry") return "damaged geometry";
  return (/^[aeiou]/.test(n) ? "an " : "a ") + n;
}

/** "3 thin gaps", "1 seam problem". */
export function countOf(type, n) {
  const one = problemName(type);
  return `${n} ${n === 1 ? one : PLURAL[one] || one}`;
}

/* THE WORDS THE CUSTOMER MAY NEVER READ. Internal search vocabulary, internal step names and every class
 * identifier; `isPlain` also refuses anything shaped like code (snake_case, CamelCase classes, dotted paths, a stack
 * trace or an exception's name). The test that scans the UI's strings (tests/ui/test_repair_words.py) reads this
 * same list, so the rule and its check cannot drift. */
export const FORBIDDEN_TERMS = [
  "tournament", "entrant", "heat", "rule seed", "seed rule", "fallback", "fall back", "conformal",
  "search_ai", "traceback", "exception", "stack trace", "occ", "brep", "b-rep", "opencascade",
  ...Object.keys(PROBLEM_WORDS).filter((k) => k.includes("_")),
];

const CODE_SHAPED = [
  /\b[a-z]+_[a-z0-9_]+\b/,                 // snake_case identifiers and op names
  /\b[A-Z][a-z]+[A-Z][A-Za-z]+(Error|Exception)?\b/,   // ClassNames, ValueError
  /\b\w+\.\w+\.\w+\b/,                     // dotted module paths
  /File "[^"]+", line \d+/,                // a Python traceback frame
  /[{}<>]|=>|\(\)/,                        // code and markup
];

/** True when `text` is safe to put in front of the customer as written. */
export function isPlain(text) {
  const t = String(text || "").trim();
  if (!t || t.length > 240) return false;
  const low = t.toLowerCase();
  if (FORBIDDEN_TERMS.some((w) => new RegExp(`(^|[^a-z])${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^a-z]|$)`).test(low))) return false;
  return !CODE_SHAPED.some((re) => re.test(t));
}

/** `text` when it is plain, else `ownWords` (this module's own sentence). */
export function plainOr(text, ownWords) {
  return isPlain(text) ? String(text).trim() : ownWords;
}

/* ---------- the choice ---------- */

/** The three options of the geometry-check form, in order, default first. `key` is the dispatch's `repair_goal`. */
export const CHOICES = [
  { key: "meshing", label: "Repair + mesh",
    help: "We fix problems in your CAD, then mesh the repaired part." },
  { key: "standalone", label: "Repair CAD only",
    help: "We fix your CAD and give you back a repaired STEP file. No mesh." },
  { key: "off", label: "Mesh only",
    help: "We mesh your CAD as it is, without repairing it." },
];

/** A goal the backend or the intake set, as one of the three keys; anything else is the default. */
export function choiceKey(goal) {
  return CHOICES.some((c) => c.key === goal) ? goal : "meshing";
}

/** The label of a goal ("Repair CAD only"). */
export function choiceLabel(goal) {
  return CHOICES.find((c) => c.key === choiceKey(goal)).label;
}

/* ---------- the live lane ---------- */

/** The repair lane's steps, in the order a repair goes through them. */
export const STEPS = ["checking", "repairing", "broader_repair", "verifying", "done"];

/** Which step a `repair_progress` event is at. The contract names the field `stage`; the wire's `stage` is also the
 *  lane, so a build that moved it to `step` or `phase` is read too. "" when it names no step this build knows. */
export function stepOf(ev) {
  const e = ev || {};
  for (const k of ["step", "phase", "repair_stage", "stage"]) if (STEPS.includes(e[k])) return e[k];
  return "";
}

const num = (v) => (Number.isFinite(Number(v)) && v !== null && v !== "" ? Number(v) : null);

/** What one problem came to: fixed | not_fixed | left_as_is, or "" when the event does not say. */
export function resultOf(ev) {
  const r = String((ev || {}).result || (ev || {}).status || (ev || {}).outcome || "");
  return ["fixed", "not_fixed", "left_as_is"].includes(r) ? r : "";
}

/** The plain words for one problem's result. */
export const RESULT_WORDS = {
  fixed: "Fixed",
  not_fixed: "Couldn't fix this one",
  left_as_is: "Left as is",
};

/** The lane's line for one `repair_progress` event: {text, index, total, step, done, fixed}. Built from the typed
 *  fields; the event's own `text` is used only where the typed fields cannot say it and it passes `isPlain`. */
export function progressLine(ev) {
  const e = ev || {}, step = stepOf(e);
  const index = num(e.problem_index), total = num(e.problem_total);
  const fixed = num(e.problems_fixed ?? e.fixed);
  const result = resultOf(e);
  let text = "";
  if (step === "checking") {
    text = total == null ? "Checking your CAD"
      : total === 0 ? "No problems found in your CAD"
        : `Found ${total} problem${total === 1 ? "" : "s"} to fix`;
  } else if (step === "repairing") {
    const what = e.problem_type ? aProblem(e.problem_type) : "a problem";
    const where = index != null && total != null ? ` — problem ${index} of ${total}` : "";
    text = result === "fixed" ? `Fixed ${what}${where}`
      : result ? `Couldn't fix ${what} — left as is${where}`
        : `Repairing ${what}${where}`;
  } else if (step === "broader_repair") {
    text = "Trying a broader repair";
  } else if (step === "verifying") {
    text = "Checking the repaired part";
  } else if (step === "done") {
    text = fixed != null && total != null ? `Repair finished: ${fixed} of ${total} fixed`
      : plainOr(e.text, "Repair finished");
  } else {
    text = plainOr(e.text, "");
  }
  return { text, index, total, step, done: step === "done", fixed, result };
}

/* ---------- the result ---------- */

/** How a problem's pin and trace row are marked: fixed | not_fixed | left_as_is (anything else is not_fixed). */
export function statusOf(p) {
  const s = String((p || {}).status || "");
  return ["fixed", "not_fixed", "left_as_is"].includes(s) ? s : "not_fixed";
}

/** The legend's words, in the order they are shown, with the CSS class each pin carries. */
export const LEGEND = [
  { key: "fixed", label: "Fixed" },
  { key: "not_fixed", label: "Not fixed" },
  { key: "left_as_is", label: "Left as is" },
];

/** The result's headline: {cls: pass|notes|fail, head, sub}, from the outcome and the choice. */
export function headlineOf(r) {
  const res = r || {}, probs = res.problems || [];
  const total = probs.length, fixed = probs.filter((p) => statusOf(p) === "fixed").length;
  const only = res.choice === "standalone";
  const after = only ? "Your repaired STEP file is ready." : "The mesh was built from the repaired part.";
  switch (res.outcome) {
    case "repaired":
      return { cls: "pass", head: "CAD repaired.", sub: total ? `All ${total} problem${total === 1 ? "" : "s"} fixed. ${after}` : after };
    case "partly_repaired":
      return { cls: "notes", head: "CAD partly repaired.", sub: `${fixed} of ${total} problems fixed. ${after}` };
    case "unchanged":
      return { cls: "notes", head: total ? "CAD left as it was." : "No problems found.",
        sub: total ? "We found problems but changed nothing. The reasons are listed below."
          : only ? "Your CAD needed no repair." : "Your CAD was meshed as it is." };
    case "unrepairable":
      return { cls: "fail", head: "We couldn't repair this CAD.", sub: "The reasons are listed below. Your original file is unchanged." };
    default:
      return { cls: "notes", head: "Repair finished.", sub: `${fixed} of ${total} problems fixed.` };
  }
}

const three = (v) => Number(Number(v).toPrecision(3)).toString();
const mm = (v) => `${three(v)} mm`;
const pct = (ratio) => {
  const d = (Number(ratio) - 1) * 100;
  if (!Number.isFinite(d)) return "";
  if (Math.abs(d) < 0.005) return "no change";
  return `${d > 0 ? "+" : ""}${Math.abs(d) < 1 ? d.toFixed(2) : d.toFixed(1)}%`;
};

/** "4 min 10 s" from seconds. */
export function secondsWords(s) {
  const t = Math.round(Number(s));
  if (!Number.isFinite(t) || t < 0) return "";
  if (t < 60) return `${t} s`;
  const m = Math.floor(t / 60), h = Math.floor(m / 60);
  if (h) return `${h} h ${m % 60} min`;
  return `${m} min${t % 60 ? ` ${t % 60} s` : ""}`;
}

/** Problems found and fixed, by plain type: [{type, name, found, fixed}], most found first. */
export function byType(r) {
  const out = new Map();
  for (const p of (r || {}).problems || []) {
    const name = problemName(p.type);
    const row = out.get(name) || { type: PROBLEM_WORDS[p.type] ? p.type : "unknown", name, found: 0, fixed: 0 };
    row.found += 1;
    if (statusOf(p) === "fixed") row.fixed += 1;
    out.set(name, row);
  }
  return [...out.values()].sort((a, b) => b.found - a.found);
}

/** The key numbers: [{k, v, sub, bad}] - the same shape the mesh result's key numbers use. */
export function repairKeyNumbers(r) {
  const res = r || {}, probs = res.problems || [], out = [];
  const fixed = probs.filter((p) => statusOf(p) === "fixed").length;
  out.push({ k: "Problems fixed", v: `${fixed} of ${probs.length}`, bad: fixed < probs.length });
  const mp = num(res.meshers_passing), mt = num(res.meshers_total);
  if (mp != null && mt) out.push({ k: "Meshers passing", v: `${mp} of ${mt}`, bad: mp < mt,
    sub: "meshing tools that accept the part" });
  const mx = num(res.max_change_mm), al = num(res.allowed_change_mm);
  if (mx != null) out.push({ k: "Largest change", v: mm(mx), sub: al != null ? `allowed ${mm(al)}` : "",
    bad: al != null && mx > al });
  const vr = num(res.volume_ratio), sr = num(res.size_ratio);
  if (vr != null) out.push({ k: "Volume change", v: pct(vr) });
  if (sr != null) out.push({ k: "Size change", v: pct(sr) });
  const took = secondsWords(res.seconds);
  if (took) out.push({ k: "Time", v: took });
  return out;
}

/** The flags the result raises, as plain sentences. A flag this build has no words for is shown only if plain. */
const FLAG_WORDS = {
  needs_review: "Some changes were larger than the automatic limit and needed your approval.",
  auto_accepted: "Larger changes were accepted automatically, as this run allowed.",
  volume_changed: "The part's volume changed noticeably. Check it before you use it.",
  size_changed: "The part's overall size changed noticeably. Check it before you use it.",
  change_over_allowed: "At least one face moved further than the allowed change.",
  meshers_failing: "Not every meshing tool accepts the repaired part.",
  spend_capped: "The repair stopped early to stay within its time and cost limit.",
  engine_switched: "A different repair method finished this job.",
};
export function flagLines(r) {
  const res = r || {}, out = [];
  for (const f of res.flags || []) {
    const key = typeof f === "string" ? f : (f && (f.key || f.code)) || "";
    const own = FLAG_WORDS[key];
    if (own) out.push(own);
    else if (typeof f === "object" && f && isPlain(f.text)) out.push(String(f.text).trim());
    else if (isPlain(key)) out.push(key);
  }
  if (res.auto_accepted && !out.includes(FLAG_WORDS.auto_accepted)) out.push(FLAG_WORDS.auto_accepted);
  // the job records which repair finished it; the customer is told only that it was a different method
  if (res.engine === "v3" && !out.includes(FLAG_WORDS.engine_switched)) out.push(FLAG_WORDS.engine_switched);
  return out;
}

/** What we did for one problem, from its status, its change and its plain reason. */
function didWords(p) {
  const s = statusOf(p), ch = num(p.change_mm);
  const moved = ch != null && ch > 0 ? ` (faces moved up to ${mm(ch)})` : "";
  if (s === "fixed") return { did: `Repaired it${moved}`, result: "Fixed" };
  if (s === "left_as_is") return { did: "Left it as is", result: "Left as is" };
  return { did: "Tried to repair it", result: "Couldn't fix this one" };
}

/** The plain trace: one row per problem - problem -> what we did -> result, and the reason when it was not fixed.
 *  [{n, id, status, problem, did, result, reason}] */
export function traceRows(r) {
  return ((r || {}).problems || []).map((p, i) => {
    const s = statusOf(p), w = didWords(p);
    const own = s === "left_as_is" ? "it was safer to leave it than to change the part"
      : s === "not_fixed" ? "no safe repair kept the part within the allowed change" : "";
    const reason = s === "fixed" ? "" : plainOr(p.reason_text, own);
    return { n: i + 1, id: String(p.id ?? i + 1), status: s, problem: problemName(p.type), did: w.did,
      result: w.result, reason };
  });
}

/** One trace row as a sentence, the way the list reads it ("Thin gap -> Repaired it -> Fixed"). */
export function traceSentence(row) {
  const head = row.problem.charAt(0).toUpperCase() + row.problem.slice(1);
  return row.status === "left_as_is" ? `${head} → left as is: ${row.reason}`
    : row.status === "not_fixed" ? `${head} → ${row.did} → ${row.result}: ${row.reason}`
      : `${head} → ${row.did} → ${row.result}`;
}

/** The deviation heatmap's legend: how far faces moved, against the allowed change. */
export function deviationLegend(r) {
  const res = r || {}, al = num(res.allowed_change_mm), mx = num(res.max_change_mm);
  return { title: "How far faces moved", top: al != null ? Math.max(al, mx || 0) : mx || 0, allowed: al,
    allowedText: al != null ? `${mm(al)} allowed` : "", maxText: mx != null ? `largest ${mm(mx)}` : "" };
}

/* ---------- the deliverables ---------- */

/** The repair's files, in the order a customer uses them, with the name the list shows. Any other type belongs to
 *  another panel. `returned_cad` is the customer's own STEP handed back unchanged; `mesh_hints` the sizing sidecar. */
export const REPAIR_FILES = [
  ["repaired_cad", "Repaired STEP"],
  ["returned_cad", "Your STEP, unchanged"],
  ["original_cad", "Original STEP"],
  ["repair_report_html", "Repair report (HTML)"],
  ["repair_report", "Repair report (JSON)"],
  ["mesh_hints", "Mesh hints"],
];
export function repairFiles(files) {
  const have = (files || []).filter((f) => f && REPAIR_FILES.some(([t]) => t === f.type));
  return REPAIR_FILES.flatMap(([t, name]) => have.filter((f) => f.type === t).map((f) => ({ ...f, name })));
}
