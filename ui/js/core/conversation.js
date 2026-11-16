// Responsibility: Say in what order a conversation and its runs are drawn - THE ONE ORDER, live and on a reload.
// Owns: which stored messages a person reads, and where each run's box goes among them.
// Boundaries: pure - no DOM, no network. main.js draws what this returns, top to bottom.

/* A RUN IS AN ITEM OF THE CONVERSATION. Live, its box is drawn at the end of the conversation the moment the run
 * starts, so it sits under the message that started it; whatever is said after the run comes after its result.
 * A reload draws the stored conversation in that same order: the server marks the message a run started after
 * with the run's id (`job_id`), and the box goes right there. A conversation stored before runs were marked has
 * no mark: its box goes after everything said, which is where it was when the run started. */

/** The messages a person reads: theirs and Hexera's - never the application's own nudge, nor a turn with no words. */
export function readable(m) {
  return !!m && typeof m === "object" && (m.role === "user" || m.role === "assistant")
    && !m._synthetic && String(m.content ?? "").trim() !== "";
}

/** What a page opened on run `jobId` draws, in order: {role, text} for a message, {run} for an earlier run of the
 *  same conversation (drawn finished, where it started), {run, current: true} for this run. Drawing stops at the
 *  message the NEXT run started after: that run, and the words that started it, are its own page. Pure. */
export function runOrder(messages, jobId) {
  const id = String(jobId || ""), out = [];
  let placed = false;
  for (const m of messages || []) {
    const tag = m && typeof m === "object" && m.job_id ? String(m.job_id) : "";
    if (placed && tag && tag !== id) break;
    if (readable(m)) out.push({ role: m.role, text: String(m.content) });
    if (tag && tag === id && !placed) { out.push({ run: id, current: true }); placed = true; }
    else if (tag && tag !== id) out.push({ run: tag });
  }
  if (!placed) out.push({ run: id, current: true });
  return out;
}
