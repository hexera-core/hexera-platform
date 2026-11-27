// Responsibility: Say in what order a conversation and its runs are drawn - THE ONE ORDER, live and on a reload.
// Owns: which stored messages a person reads, and where each run's box goes among them.
// Boundaries: pure - no DOM, no network. main.js draws what this returns, top to bottom.

/* A RUN IS AN ITEM OF THE CONVERSATION. Live, its box is drawn at the end of the conversation the moment the run
 * starts, so it sits under the message that started it; whatever is said after the run comes after its result.
 * A reload draws the stored conversation in that same order: the server marks the message a run started after
 * with the run's id (`job_id`), and the box goes right there. A conversation stored before runs were marked has
 * no mark: its box goes after everything said, which is where it was when the run started. */

/** The messages a person reads: theirs and Hexera's - never the application's own nudge, nor a turn with nothing in
 *  it (a user's turn may be a picture alone, or a face picked). */
export function readable(m) {
  return !!m && typeof m === "object" && (m.role === "user" || m.role === "assistant")
    && !m._synthetic && (String(m.content ?? "").trim() !== "" || !!carried(m));
}

/** What a user's turn carried beside its words ({attachments, faces}), or null. Pure. */
export function carried(m) {
  if (!m || m.role !== "user") return null;
  const attachments = Array.isArray(m.attachments) ? m.attachments : [];
  const faces = Array.isArray(m.faces) ? m.faces : [];
  return attachments.length || faces.length ? { attachments, faces } : null;
}

function item(m) {
  const c = carried(m);
  return c ? { role: m.role, text: String(m.content ?? ""), extra: c } : { role: m.role, text: String(m.content) };
}

/** What a page reopened on an unfinished conversation draws, in order: {role, text} for each message a person
 *  reads - the same messages, in the same order, as a run's page draws before its box. Pure. */
export function intakeOrder(messages) {
  return (messages || []).filter(readable).map(item);
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
    if (readable(m)) out.push(item(m));
    if (tag && tag === id && !placed) { out.push({ run: id, current: true }); placed = true; }
    else if (tag && tag !== id) out.push({ run: tag });
  }
  if (!placed) out.push({ run: id, current: true });
  return out;
}
