// Responsibility: Turn a mesh run's events into one live line at the top of the run card - the step
// running now in plain words (the engine chosen and why, the plan, meshing on the cloud mesher and
// what the mesher says it is doing, checking quality, the AI review), its time, the steps done
// ticked with their times, a note when a step runs long, and how the run ended.
// Boundaries: a model the stage renderer feeds (render/stage.js); it draws through the geometry
// check's line (render/check_progress.js), so the two read alike. It reads no prose to decide
// anything: an event's TYPE and lane say what happened; a note is only ever shown as it was said.
import { createCheckProgress, durationWords } from "./check_progress.js";

const STEPS = [
  ["engine", "Choosing the mesh engine…", "Chose the mesh engine"],
  ["plan", "Planning the mesh…", "Planned the mesh"],
  ["mesh", "Meshing on the cloud mesher…", "Meshed"],
  ["quality", "Checking quality…", "Checked quality"],
  ["review", "Reviewing the mesh against your brief…", "Reviewed"],
];
/* which lane opening starts which step */
const LANE_STEP = { engine_select: "engine", builder: "plan", executor: "quality", classifier: "quality",
                    reviewer: "review" };
/* a step this long with no word from the run gets a note: it is still running, and the page will move */
const QUIET_NOTE_MS = 10 * 60 * 1000;
/* how long a step may run before its line says so - the mesh's own is the engine's measured time */
const SOFT_MS = { engine: 60e3, plan: 5 * 60e3, quality: 3 * 60e3, review: 5 * 60e3 };

export function createRunProgress(now = () => Date.now()) {
  let steps, attempt, waitingText, ended, meshInfo, lastEventAt;
  function reset() {
    steps = STEPS.map(([key, doing, label]) => ({ key, doing, label, state: "todo", seconds: null, t0: null }));
    attempt = 1; waitingText = ""; ended = null; meshInfo = null; lastEventAt = 0;
  }
  reset();
  const step = (k) => steps.find((s) => s.key === k);
  const idx = (k) => steps.findIndex((s) => s.key === k);

  function finish(s, at) {
    if (s.state !== "active") return;
    s.state = "done"; s.seconds = Math.max(0, (at - s.t0) / 1000);
  }
  /** `key` starts now: every step before it that was open is done, and any it skipped is done too
   *  (a pinned engine opens no engine lane; an engine without a planner sends no plan). */
  function start(key, at) {
    const i = idx(key);
    steps.forEach((s, j) => {
      if (j < i) { if (s.state === "active") finish(s, at); else if (s.state === "todo") s.state = "done"; }
      // a step run again (a second meshing pass) makes the steps after it to do again
      else if (j > i && s.state !== "todo") Object.assign(s, { state: "todo", seconds: null, t0: null, label: STEPS[j][2] });
    });
    const s = steps[i];
    if (s.state === "active" || (key === "engine" && s.state === "done")) return s;
    Object.assign(s, { state: "active", t0: at, seconds: null });
    return s;
  }
  const touch = (at) => { lastEventAt = Math.max(lastEventAt, at); };

  return {
    reset,
    lane(agent, at) { touch(at); const k = LANE_STEP[agent]; if (k) start(k, at); },
    waiting(text) { waitingText = text || ""; },
    /** the engine lane's own words: "Mesh engine: cfMesh - it did best in our lab on shapes like this one" */
    engine(text, at) {
      touch(at); const s = start("engine", at);
      if (/^Mesh engine:/.test(String(text || ""))) { s.label = String(text).trim(); s.engine = true; }
    },
    plan(summary, at) {
      touch(at); const s = start("plan", at);
      if (summary) s.label = "Plan: " + summary;
      finish(s, at);
    },
    meshing(engine, budgetS, history, at) {
      touch(at); start("mesh", at);
      const e = step("engine"); if (!e.engine && engine) e.label = "Mesh engine: " + engine;
      meshInfo = { budget: Number(budgetS) || 0, typical: (history && history.typical_s) || 0, stage: "" };
    },
    meshStage(label, at) { touch(at); if (meshInfo) meshInfo.stage = String(label || ""); },
    meshed(cells, at) {
      touch(at); const s = step("mesh");
      if (s.state !== "active") start("mesh", at);
      if (cells) s.label = "Meshed - " + cells;
      finish(s, at); meshInfo = null;
    },
    verdict(ok, at) {
      touch(at); const s = step("review");
      if (s.state !== "active") start("review", at);
      s.label = ok ? "Review passed" : "Review asked for changes";
      finish(s, at);
    },
    /** a rebuild: the plan, the mesh and the checks run again; the engine stands */
    attempt(n, at) {
      touch(at);
      if (!(n > attempt)) return;
      attempt = n;
      steps.forEach((s) => { if (s.key !== "engine") Object.assign(s, { state: "todo", seconds: null, t0: null, label: STEPS[idx(s.key)][2] }); });
      meshInfo = null;
    },
    final(data, at) {
      const failed = !data.pass && !data.cancelled;
      steps.forEach((s) => {
        if (s.state === "active") { if (failed) { s.state = "failed"; s.seconds = (at - s.t0) / 1000; } else finish(s, at); }
      });
      ended = { pass: !!data.pass, cancelled: !!data.cancelled, concerns: !!data.concerns, notes: !!data.notes,
                total: data.totalMs != null ? data.totalMs / 1000 : null };
    },
    /** The line as check_progress draws it. */
    view() {
      const t = now();
      const shown = steps.map((s) => ({ key: s.key, doing: s.doing, label: s.label, state: s.state,
        seconds: s.state === "active" ? null : s.seconds }));
      const prefix = attempt > 1 ? `Attempt ${attempt}: ` : "";
      if (ended) {
        const total = ended.total;
        if (ended.pass) return { state: "done", live: false, steps: shown,
          current: (ended.concerns ? "Mesh delivered with concerns" : ended.notes ? "Mesh ready, with notes" : "Mesh ready")
            + (total != null ? ` in ${durationWords(total)}` : "") };
        if (ended.cancelled) return { state: "failed", live: false, steps: shown, current: "Run cancelled.",
          advice: "", can_retry: false };
        const at = steps.find((s) => s.state === "failed");
        return { state: "failed", live: false, steps: shown,
          current: at ? `Stopped while ${at.doing.replace(/…$/, "").replace(/^./, (c) => c.toLowerCase())}.` : "The run ended without a mesh.",
          advice: "The message below says what happened. Tell me what to change, or run it again.", can_retry: false };
      }
      const active = steps.find((s) => s.state === "active");
      if (!active) {
        const started = steps.some((s) => s.state !== "todo");
        return { state: "queued", live: true, steps: shown,
          current: started ? prefix + "Getting the next step ready…" : "Waiting for a worker…", note: started ? "" : waitingText,
          elapsed_s: null };
      }
      const elapsed = Math.max(0, (t - active.t0) / 1000);
      let current = prefix + active.doing, note = "";
      if (active.key === "mesh" && meshInfo) {
        if (meshInfo.stage) current = `${prefix}Meshing on the cloud mesher - ${meshInfo.stage}…`;
        const typ = meshInfo.typical, cap = meshInfo.budget;
        if (cap && elapsed >= cap) note = "Past its time limit - it should stop by itself any moment now.";
        else if (cap && elapsed >= 0.8 * cap) note = `If it's still going at ${durationWords(cap)}, it stops by itself.`;
        else if (typ && elapsed > typ) note = `Taking longer than similar runs (usually about ${durationWords(typ)}).`;
      } else if (SOFT_MS[active.key] && elapsed * 1000 > SOFT_MS[active.key]) {
        note = "Taking longer than usual - still working.";
      }
      if (!note && lastEventAt && t - lastEventAt > QUIET_NOTE_MS && active.key !== "mesh") {
        note = "No news from the run for a while. It's still running; this updates when it moves.";
      }
      return { state: "running", live: true, steps: shown, current, note, elapsed_s: elapsed,
               slow: note ? "yes" : undefined };
    },
  };
}

/** The run card's line: the model above, drawn at the top of the card. */
export function createRunLine() {
  const model = createRunProgress();
  const line = createCheckProgress();
  let host = null;
  const api = {
    model,
    attach(el) { host = el; },
    draw() { if (host && host.isConnected) line.show(host, model.view(), { pinned: true, id: "run-progress" }); },
    reset() { model.reset(); line.reset(); host = null; },
  };
  return api;
}
