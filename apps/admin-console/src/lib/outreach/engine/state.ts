/**
 * Enrollment state machine.
 *
 * Every status change in the system goes through `transition`. Centralizing it
 * buys three things that matter for something that emails real people:
 * illegal transitions are rejected rather than silently applied, terminal
 * states genuinely stick (a stopped sequence cannot be resurrected by a stray
 * scheduler pass), and every change lands in the audit log.
 *
 *   pending ──► active ──► completed        all steps sent, silence
 *      │          │
 *      │          ├──────► replied          a human answered
 *      │          ├──────► stopped          negative reply, or stopped by hand
 *      │          ├──────► bounced          hard delivery failure
 *      │          └──────► review           needs a human decision
 *      └─────────────────► suppressed       blocked before the first send
 */
import { get, run, transaction } from "../db";
import { nowIso } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import type { Enrollment, EnrollmentStatus } from "../core/types";

const ALLOWED: Record<EnrollmentStatus, EnrollmentStatus[]> = {
  pending: ["active", "suppressed", "stopped", "review", "bounced", "replied"],
  active: ["active", "completed", "replied", "bounced", "stopped", "review", "suppressed"],
  // Terminal states. `review` is the one that can be re-opened, and only by a
  // human acting through the dashboard.
  completed: ["replied", "review"],
  replied: ["review", "stopped"],
  bounced: [],
  stopped: ["review"],
  review: ["active", "stopped", "replied", "completed", "suppressed"],
  suppressed: ["review"],
};

export class IllegalTransitionError extends Error {
  constructor(from: EnrollmentStatus, to: EnrollmentStatus) {
    super(`Illegal enrollment transition: ${from} → ${to}`);
    this.name = "IllegalTransitionError";
  }
}

export function canTransition(from: EnrollmentStatus, to: EnrollmentStatus): boolean {
  return ALLOWED[from]?.includes(to) ?? false;
}

export interface TransitionOptions {
  reason?: string;
  nextSendAt?: string | null;
  currentStep?: number;
  threadId?: string | null;
  lastMessageId?: string | null;
  /** Who or what caused this — 'worker', 'human', 'reply-classifier'. */
  actor?: string;
}

export async function transition(
  enrollmentId: number,
  to: EnrollmentStatus,
  options: TransitionOptions = {},
): Promise<Enrollment> {
  return await transaction(async (client) => {
    const current = await get<Enrollment>(`SELECT * FROM enrollments WHERE id = ?`, [enrollmentId], client);
    if (!current) throw new Error(`Enrollment ${enrollmentId} not found`);

    if (current.status !== to && !canTransition(current.status, to)) {
      throw new IllegalTransitionError(current.status, to);
    }

    // Anything terminal must stop being scheduled. Relying on each call site
    // to remember this is how a "stopped" contact gets another email.
    const terminal = to !== "pending" && to !== "active";
    const nextSendAt = terminal ? null : (options.nextSendAt ?? current.next_send_at);

    await run(
      `UPDATE enrollments SET
         status = ?, next_send_at = ?, current_step = ?, thread_id = ?,
         last_message_id = ?, stopped_reason = ?, updated_at = ?
       WHERE id = ?`,
      [
        to,
        nextSendAt,
        options.currentStep ?? current.current_step,
        options.threadId !== undefined ? options.threadId : current.thread_id,
        options.lastMessageId !== undefined ? options.lastMessageId : current.last_message_id,
        options.reason ?? current.stopped_reason,
        nowIso(),
        enrollmentId,
      ], client);

    if (current.status !== to) {
      recordEvent({
        type: EVENT_TYPES.enrollmentStatusChanged,
        entityType: "enrollment",
        entityId: enrollmentId,
        campaignId: current.campaign_id,
        contactId: current.contact_id,
        payload: {
          from: current.status,
          to,
          reason: options.reason ?? null,
          actor: options.actor ?? "worker",
        },
      });
    }

    // The `!` belongs to the RESOLVED row, not the promise: the row was just updated inside
    // this transaction, so it exists.
    return (await get<Enrollment>(`SELECT * FROM enrollments WHERE id = ?`, [enrollmentId], client))!;
  });
}

/**
 * Halts a sequence. The single entry point used by the reply classifier, the
 * bounce handler and the dashboard's stop button, so "stopped" always means
 * the same thing and is always recorded the same way.
 */
export async function stopSequence(
  enrollmentId: number,
  status: Extract<EnrollmentStatus, "stopped" | "replied" | "bounced" | "review" | "suppressed">,
  reason: string,
  actor = "worker",
): Promise<Enrollment> {
  return await transition(enrollmentId, status, { reason, nextSendAt: null, actor });
}
