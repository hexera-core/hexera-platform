// Presentation for a run's two derived columns. Pure, so the table's behaviour is testable
// without rendering it.

const MARKS: Record<string, string> = {
  succeeded: "status status--ok",
  failed: "status status--fail",
  running: "status status--run",
};

/** The class a status word wears. Meaning is carried by the WORD as well as the mark; the class
 *  only adds the mark, and an unrecognised status still renders as a neutral row rather than
 *  breaking the table. JobStatus can gain a member without this file changing. */
export function statusClass(status: string): string {
  return MARKS[status] ?? "status";
}

/** How long a finished run took, in whole units.
 *
 * An em dash for anything that has not both started and ended, and for a negative span: clock
 * skew between the API host and the worker can record an end before a start, and "-3s" in a
 * table reads as a bug in the product rather than as a bug in a clock.
 */
export function formatDuration(startedIso: string | null, endedIso: string | null): string {
  if (!startedIso || !endedIso) {
    return "—";
  }
  const seconds = Math.round(
    (new Date(endedIso).getTime() - new Date(startedIso).getTime()) / 1000,
  );
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "—";
  }
  if (seconds < 60) {
    return `${seconds}s`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

/** What a run is called in a list.
 *
 * A dispute creates a FRESH job and links no conversation to it, so the label the run list joins
 * through `ChatSession` is null for every re-review. Falling through to "Untitled study" made
 * them all identical and made a deliberate re-run look like a nameless one. The API says which
 * is which via `is_rerun`; this says it in words.
 */
export function runTitle(run: { task_label: string | null; is_rerun?: boolean }): string {
  if (run.task_label) {
    return run.task_label;
  }
  return run.is_rerun ? "Re-review of an earlier run" : "Untitled study";
}
