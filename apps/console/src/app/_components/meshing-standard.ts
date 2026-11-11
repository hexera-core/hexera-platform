/**
 * The team's meshing standard as the product API returns it (GET/PUT /organization/meshing-standard), and every
 * plain sentence the Meshing settings page says about it. Pure, so each sentence is tested where it is written.
 *
 * `lines` are the reader's own lines, word for word as a run says them ("Global element size 10 mm.", "Not
 * applied: ..."): the page shows them as given and never rewrites one.
 */
export type MeshingStandard = {
  text: string;
  lines: string[];
  status: "read" | "unread" | "empty";
  updated_by: string | null;
  updated_at: string | null;
};

/** The most characters a standard may hold: every one of them is read (engines/meshing_standard.MAX_CHARS). */
export const MAX_CHARS = 8000;

/** What the box shows before anything is typed: a short list the way an engineer writes one. */
export const PLACEHOLDER = [
  "Global Mesh Size = 10 mm",
  "Aspect Ratio < 4.0 (1% between 4.0 and 6.0 is acceptable)",
  "Jacobian > 0.6",
  "Quads only",
].join("\n");

/** The one line under the box: where the standard applies and what beats it. */
export const APPLIES = "Applied to every run. What you write in a run's notes wins for that run.";

/** The one line that says whose standard this is. */
export const SCOPE = "One standard for your whole team, shared by everyone in this organisation.";

/** How tall the box is: the whole standard in view (a pasted criteria list), between 8 and 24 lines. */
export function rowsFor(text: string): number {
  return Math.min(24, Math.max(8, text.split("\n").length + 1));
}

/** A "Not applied" line from the reader: shown quieter than a line that applies. */
export function isNotApplied(line: string): boolean {
  return line.startsWith("Not applied");
}

/**
 * The one sentence for a saved standard with no lines to show, or null when the lines say it all (or nothing is
 * saved: the box's example and the line under it say what to do). An unread standard was saved while the AI step
 * that reads it gave no answer: each run reads it when it starts.
 */
export function statusLine(standard: MeshingStandard): string | null {
  if (standard.status === "empty") {
    return null;
  }
  if (standard.status === "unread") {
    return "Saved. We couldn't read it just now, so each run reads it when it starts.";
  }
  return standard.lines.length === 0
    ? "Saved. Nothing in it is a mesh setting we can apply."
    : null;
}

/** Who saved the standard last, and when ("Saved 11 Nov 2026 by engineer@example.com."), or null. */
export function savedBy(standard: MeshingStandard): string | null {
  if (standard.status === "empty" || !standard.updated_at) {
    return null;
  }
  const at = new Date(standard.updated_at);
  if (Number.isNaN(at.getTime())) {
    return null;
  }
  const day = at.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
    year: "numeric",
  });
  return standard.updated_by ? `Saved ${day} by ${standard.updated_by}.` : `Saved ${day}.`;
}

/**
 * A failed save, in plain words. 403: the caller is acting on an API key (the standard is a console session's to
 * change); 404: the account has no organisation to hold one; 422: longer than the reader reads.
 */
export function saveFailureMessage(status: number): string {
  if (status === 403) {
    return "Only a console session can change the standard.";
  }
  if (status === 404) {
    return "Your account has no organisation to hold a standard.";
  }
  if (status === 422) {
    return `That is longer than ${MAX_CHARS.toLocaleString("en-GB")} characters. Shorten it and save again.`;
  }
  return "Could not save just now. Try again.";
}
