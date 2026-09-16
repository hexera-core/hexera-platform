/**
 * When a message is handed over, as opposed to whether it may be.
 *
 * Two modes, and they are genuinely different machines rather than one
 * machine with a flag:
 *
 *   manual     There are no timers. Every enrolled contact in an active
 *              campaign is ready the moment it is enrolled. You tick the ones
 *              you want and press send, at whatever hour suits you. Nothing
 *              goes out on its own, ever.
 *
 *   automatic  The scheduler's timings apply: step delays, the campaign send
 *              window, and a start time for the day. The worker sends on its
 *              own from that time onward.
 *
 * The scheduled time is automatic mode's business. Showing a countdown to
 * someone who is about to pick recipients by hand and press a button is
 * theatre, so manual mode ignores next_send_at entirely.
 *
 * What manual mode does NOT skip: the two safety switches, the daily caps,
 * one person per company per day, suppression, and the rule that a follow-up
 * never fires after a reply. Those protect the sending domain and the people
 * on the list, not the schedule.
 */
import {
  dueEnrollments,
  readyEnrollments,
  enrollmentsByIds,
  type DueItem,
} from "./scheduler";
import { sendStep } from "./sender";
import { getSetting, SETTING_KEYS } from "../core/settings";
import { zonedParts } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import type { Campaign } from "../core/types";

export type DispatchMode = "manual" | "automatic";

export async function dispatchMode(): Promise<DispatchMode> {
  return await getSetting(SETTING_KEYS.dispatchMode) === "automatic" ? "automatic" : "manual";
}

export async function isManual(): Promise<boolean> {
  return (await dispatchMode()) === "manual";
}

/** "HH:MM" in the campaign's timezone. Falls back to 09:00 if malformed. */
export async function dispatchTime(): Promise<{ hour: number; minute: number; text: string }> {
  const raw = await getSetting(SETTING_KEYS.dispatchTime) || "09:00";
  const match = /^(\d{1,2}):(\d{2})$/.exec(raw.trim());
  const hour = match ? Number(match[1]) : 9;
  const minute = match ? Number(match[2]) : 0;
  const valid = hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59;
  const h = valid ? hour : 9;
  const m = valid ? minute : 0;
  return { hour: h, minute: m, text: `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}` };
}

/**
 * Everything you could send right now.
 *
 * In manual mode that is every enrolled contact in an active campaign, with no
 * regard for the clock. In automatic mode it is whatever the scheduler says is
 * due. The Queue page renders this, so the two modes show genuinely different
 * lists rather than the same list with a disabled button.
 */
export async function sendableNow(now = new Date()): Promise<DueItem[]> {
  return (await isManual()) ? readyEnrollments() : dueEnrollments(now);
}

export interface DispatchGate {
  allowed: boolean;
  reason: string;
}

/** May the worker send this campaign's mail unattended right now? */
export async function autoDispatchGate(campaign: Campaign, now = new Date()): Promise<DispatchGate> {
  if (await isManual()) {
    return { allowed: false, reason: "manual mode, nothing sends on its own" };
  }

  const { hour, minute, text } = await dispatchTime();
  const local = zonedParts(now, campaign.timezone);
  const released = local.hour > hour || (local.hour === hour && local.minute >= minute);

  return released
    ? { allowed: true, reason: `automatic, released at ${text}` }
    : { allowed: false, reason: `automatic, but today's run does not start until ${text}` };
}

export interface DispatchSummary {
  attempted: number;
  sent: number;
  previewed: number;
  skipped: number;
  stopped: number;
  completed: number;
  failed: number;
  /** Due, but the gate said not yet. Always 0 on a manual run. */
  held: number;
  heldReason: string | null;
  errors: string[];
  skippedReasons: string[];
}

export interface DispatchOptions {
  /** "manual" comes from a button press: it ignores the start time and the
   *  send window, because a person chose this moment on purpose. */
  trigger: "manual" | "automatic";
  now?: Date;
  /** Send exactly these enrollments. Manual only; without it a manual run
   *  would mean "everything", which is not a thing any button here does. */
  enrollmentIds?: number[];
  limit?: number;
}

/**
 * Run the sends.
 *
 * Nothing here decides whether mail is real. That is the two-switch interlock
 * in lib/core/settings.ts, checked inside sendStep, so pressing the button
 * while either switch is off rehearses rather than sends.
 */
// One dispatch at a time, enforced in-process. A double-click on Send once
// spawned three concurrent runs over the same ready list; each was correct
// alone and together they triple-mailed people. Concurrent callers get an
// empty summary that says so instead of a second run.
let dispatchInFlight = false;

export async function runDispatch(options: DispatchOptions): Promise<DispatchSummary> {
  const now = options.now ?? new Date();
  const manual = options.trigger === "manual";
  const summary: DispatchSummary = {
    attempted: 0, sent: 0, previewed: 0, skipped: 0, stopped: 0,
    completed: 0, failed: 0, held: 0, heldReason: null, errors: [], skippedReasons: [],
  };

  if (dispatchInFlight) {
    summary.heldReason = "another dispatch is already running — this click did nothing";
    return summary;
  }
  dispatchInFlight = true;
  try {
    return await runDispatchInner(options, now, manual, summary);
  } finally {
    dispatchInFlight = false;
  }
}

async function runDispatchInner(
  options: DispatchOptions,
  now: Date,
  manual: boolean,
  summary: DispatchSummary,
): Promise<DispatchSummary> {

  const items: DueItem[] = manual
    ? await enrollmentsByIds(options.enrollmentIds ?? [])
    : await dueEnrollments(now, options.limit ?? 200);

  for (const item of items) {
    if (!manual) {
      const gate = autoDispatchGate(item.campaign, now);
      if (!(await gate).allowed) {
        summary.held++;
        summary.heldReason = (await gate).reason;
        continue;
      }
    }

    summary.attempted++;
    try {
      const outcome = await sendStep(item, { now, ignoreWindow: manual });
      switch (outcome.kind) {
        case "sent": summary.sent++; break;
        case "preview": summary.previewed++; break;
        case "skipped":
          summary.skipped++;
          summary.skippedReasons.push(`${item.contact.company}: ${outcome.reason}`);
          break;
        case "stopped": summary.stopped++; break;
        case "completed": summary.completed++; break;
        case "failed":
          summary.failed++;
          summary.errors.push(`${item.contact.email_normalized}: ${outcome.error}`);
          break;
      }
    } catch (error) {
      summary.failed++;
      summary.errors.push(
        `${item.contact.email_normalized}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  // Only log a run that did something. An automatic tick that held everything
  // back is the normal state for most of the day, and recording it every
  // minute would bury the events that matter.
  if (summary.attempted > 0) {
    await recordEvent({
      type: EVENT_TYPES.dispatchRun,
      entityType: "worker",
      payload: { trigger: options.trigger, ...summary },
    });
  }

  return summary;
}
