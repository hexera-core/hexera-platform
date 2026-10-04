// Responsibility: Decide what a backend event means and which renderer call it becomes.
// Boundaries: no DOM, no socket, no fetch - the renderer is injected, so a test fake runs the same dispatch.

/* THE ONE event boundary. Live events and replayed events both arrive here.
 *
 * This module decides WHAT a backend event means and WHICH renderer call it becomes. It does
 * not touch the DOM, does not open a socket and does not fetch anything - the renderer is
 * passed in, so the same dispatch runs against the real stage and against a
 * recording fake in a test. That injection is also what removes the old renderer -> viewer
 * cycle: nothing here knows a viewer exists.
 *
 * The wire is TYPED. The backend says what happened; the UI renders it. There is no prose to
 * classify here and no engine vocabulary to know, which is what keeps the whole directory
 * liftable onto a CDN.
 */
import { fmtBytes } from "./format.js";

/** Stage lanes, in the order the pipeline runs them. Presentation only - a stage the UI has
 *  no word for still renders, via `laneLabel`. */
const LANE_LABELS = {
  intake: "Requirements", engine_select: "Engine selection",
  geometry_admission: "Input validation", builder: "Mesh creation",
  executor: "Mesh validation", classifier: "Diagnostics",
  reviewer: "Review", outcome: "Outcome", result: "Result",
};

/** A stage with no label must NEVER render as a raw key. The backend owns the stage list;
 *  this degrades an unknown one to readable English instead. */
export function laneLabel(agent) {
  return LANE_LABELS[agent]
    || String(agent).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** The verdict line the timeline shows. Kept here, not in the renderer, because it is a
 *  statement about the run rather than a piece of layout. Module-private: only the `verdict`
 *  renderer below uses it. */
function verdictText(verdict) {
  return verdict === "PASS"
    ? "Verdict: the mesh meets your brief"
    : "Verdict: the reviewer left points open (see the outcome)";
}

/** What a failed verdict turns out to have been once a rebuild follows it: a request for changes,
 *  not the run's last word. The renderer rewords the verdict line with it when the next attempt
 *  opens, and marks the review's lane "sent back" rather than ticking it. */
export const SENT_BACK = "Verdict: sent back for a rebuild - the reviewer asked for changes";

/** How many attempts a finished run made, as its timeline counts them. Only an attempt in which a
 *  mesher RAN counts - the `meshing` event is published just before the mesher starts, after every
 *  refusal - so a run whose every attempt was refused before meshing says that nothing was built,
 *  where it used to say "2 attempts". `opened` is whether the timeline saw any attempt at all; when
 *  it saw none (an older run, a replay that came back empty) the server's count is all there is.
 *  Pure: (opened, built, serverCount) -> the words beside the verdict. */
export function attemptsLabel(opened, built, serverCount) {
  const n = opened ? built : Number(serverCount) || 0;
  if (n > 0) return `${n} attempt${n !== 1 ? "s" : ""}`;
  return opened ? "no mesh was built" : "";
}

/** Which lane an event belongs to, given the lane the stream was last in.
 *  Pure: (event, cursor) -> {stage, cursor, closed}. `closed` is the lane this event
 *  retires, if any - a stage becoming active closes the one before it. */
function resolveStage(ev, cursor) {
  const stage = ev.stage || cursor;
  if (ev.stage && ev.stage !== cursor) {
    const closed = cursor && cursor !== "result" ? cursor : null;
    return { stage, cursor: ev.stage, closed };
  }
  return { stage, cursor, closed: null };
}

/** Every event type this UI knows how to render, and what it does with it.
 *
 *  A new backend event needs an entry here and a renderer method - not an edit to several
 *  separate literal inventories. This registry is the whole inventory; there is no second
 *  list of type names anywhere, so none can fall out of step with it. */
const HANDLERS = {
  attempt:     (ev, r) => r.attempt(ev.n),
  stage:       (ev, r, st) => r.node(ev.stage || st),
  check:       (ev, r, st) => r.check(st, ev.statement, ev.ok),
  // the tone travels too: an error marks its lane failed, a warning marks it warned
  note:        (ev, r, st) => r.info(st, ev.text, ev.tone !== "info", ev.tone),
  action:      (ev, r, st) => (ev.actions || []).forEach((a) => r.tool(st, a)),
  search:      (ev, r, st) => r.mcp(st, ev.query),
  screenshot:  (ev, r) => { if (ev.image) r.screenshot(ev.image); },   // replayed: no bytes
  file:        (ev, r, st) => r.file(st, ev.display_path, fmtBytes(ev.byte_count), ev.operation),
  reasoning:   (ev, r, st) => r.reasoning(st, ev),
  tool_call:   (ev, r, st) => r.toolCall(st, ev),
  tool_result: (ev, r, st) => r.toolResult(st, ev),
  rationale:   (ev, r, st) => r.rationale(st, ev),
  meshing:     (ev, r, st) => { r.node(st); r.startMesh(ev.engine, ev.budget_s, ev.history); },
  meshed:      (ev, r) => r.endMesh(ev.cells ? ev.cells.toLocaleString() + " cells" : ""),
  // the verdict is kept on its lane, so the lane's mark can never contradict it
  verdict:     (ev, r, st) => r.verdict(st, ev.verdict === "PASS", verdictText(ev.verdict)),
  closing:     () => {},          // the stream holds it for the result card
};

/** What a reasoning card's header says, given the event that produced it.
 *
 *  Three named states plus a neutral one for a phase this build does not know. The unknown case
 *  must not be resolved to a lifecycle it did not claim, or the card reads "Thinking complete"
 *  for a round that never completed. Nothing is estimated - a duration or a token count that is
 *  not in the event does not appear, because an invented "8.4s" is worse than silence. */
export function reasoningHeader(ev) {
  const done = ev.phase === "completed", failed = ev.phase === "failed";
  const running = ev.phase === "started";
  const bits = [];
  if (typeof ev.duration_ms === "number") bits.push((ev.duration_ms / 1000).toFixed(1) + "s");
  // REASONING tokens only - the backend never puts another token metric here
  if (typeof ev.token_count === "number") bits.push(ev.token_count.toLocaleString() + " tokens");
  const text = failed ? "Thinking interrupted"
    : done ? (bits.length ? "Thinking · " + bits.join(" · ") : "Thinking complete")
      : running ? "Thinking…"
        : "Thinking";
  return { text, running, measured: done || failed };
}

/** Which result surface a finished run earns. Kept out of the renderer because it is a
 *  statement about what was PROVED, not about layout:
 *
 *    mesh        - it passed; the delivered mesh is the result
 *    unreviewed  - the retry loop gave up, but a mesh reached the REVIEWER and got a verdict,
 *                  so it passed every executor VALIDITY gate and only the quality bar was not
 *                  met. The real verdict is the proof of that.
 *    none        - a terminal failure. No valid mesh was ever produced; any file left in the
 *                  workspace is structurally INVALID, and showing it as "your mesh" would be a
 *                  lie. The outcome message is the whole result.
 */
export function resultSurface(data) {
  if (!data) return "none";
  if (data.pass) return "mesh";
  if (data.verdict && data.meshAvailable) return "unreviewed";
  return "none";
}

/** Route one event to the renderer. Returns the new lane cursor.
 *
 *  An unknown type is IGNORED, not thrown: the backend may publish an event this build has
 *  never seen, and a timeline that stops rendering because of one unrecognised frame is a
 *  worse failure than one that skips it. */
export function dispatch(ev, renderer, cursor) {
  if (!ev || typeof ev !== "object" || typeof ev.type !== "string") return cursor;
  const { stage, cursor: next, closed } = resolveStage(ev, cursor);
  if (closed) renderer.done(closed);
  const handler = HANDLERS[ev.type];
  if (handler) handler(ev, renderer, stage);
  return next;
}
