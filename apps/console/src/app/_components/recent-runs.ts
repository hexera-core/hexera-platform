// The sidebar's recent-runs list, as data. Pure, so the list's behaviour is testable without
// rendering it.

import { runTitle, statusLabel } from "./run-status";

export type RecentRun = {
  id: string;
  status: string;
  task_label: string | null;
  is_rerun?: boolean;
  created_at: string | null;
  review_outcome?: string | null;
};

/** How many runs the sidebar lists. The full table lives on /runs. */
export const RECENT_LIMIT = 8;

/** How often the list is read again while the page is open, so a run started in the workbench
 *  (main.js, outside React) shows up and a running dot turns into its result. */
export const RECENT_POLL_MS = 20_000;

/** The states that are still moving. Their dot pulses. */
const LIVE = new Set(["running", "pending", "queued", "pending_review"]);

/** The dot a run wears: ok, warn, bad, live or plain. Meaning is also in the row's title, never
 *  the colour alone. */
export function runDot(status: string, reviewOutcome?: string | null): "ok" | "warn" | "bad" | "live" | "" {
  if (status === "succeeded") {
    return reviewOutcome === "delivered_with_concerns" ? "warn" : "ok";
  }
  if (status === "failed") {
    return "bad";
  }
  return LIVE.has(status) ? "live" : "";
}

/** How long ago a run started, in the shortest honest words: "now", "12 min", "3 h", "2 d",
 *  "5 w". An empty string when the time is missing or in the future by more than a minute
 *  (clock skew), so the row never shows a negative age. */
export function ageWords(iso: string | null, now: number): string {
  if (!iso) {
    return "";
  }
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then) || !Number.isFinite(now)) {
    return "";
  }
  const s = (now - then) / 1000;
  if (s < -60) {
    return "";
  }
  if (s < 60) {
    return "now";
  }
  const m = Math.floor(s / 60);
  if (m < 60) {
    return `${m} min`;
  }
  const h = Math.floor(m / 60);
  if (h < 24) {
    return `${h} h`;
  }
  const d = Math.floor(h / 24);
  return d < 14 ? `${d} d` : `${Math.floor(d / 7)} w`;
}

/** One row of the list: where it links, what it says and how it is marked. */
export function recentRow(run: RecentRun, now: number) {
  return {
    href: `/runs/${encodeURIComponent(run.id)}`,
    title: runTitle({ task_label: run.task_label, is_rerun: run.is_rerun }),
    dot: runDot(run.status, run.review_outcome),
    // the dot's meaning in words, for a screen reader and the hover title
    words: statusLabel(run.status, run.review_outcome),
    age: ageWords(run.created_at, now),
  };
}
