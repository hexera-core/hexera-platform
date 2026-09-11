/**
 * The audit spine. Every meaningful state change writes exactly one event.
 *
 * The dashboard's analytics are derived from this table rather than from
 * mutable columns, which means any number on screen can be traced back to the
 * exact transitions that produced it — and a bug in a status column can never
 * silently rewrite history.
 */
import { run, all } from "../db";
import { nowIso } from "./time";
import type { OutreachEvent } from "./types";

export const EVENT_TYPES = {
  contactImported: "contact.imported",
  contactUpdated: "contact.updated",

  verificationChecked: "verification.checked",
  verificationFailed: "verification.failed",

  enrollmentCreated: "enrollment.created",
  enrollmentStatusChanged: "enrollment.status_changed",
  enrollmentStepAdvanced: "enrollment.step_advanced",
  enrollmentCompleted: "enrollment.completed",

  messageQueued: "message.queued",
  messageSent: "message.sent",
  messageDryRun: "message.dry_run",
  messageFailed: "message.failed",
  messageSkipped: "message.skipped",

  replyReceived: "reply.received",
  replyClassified: "reply.classified",
  replyReviewed: "reply.reviewed",

  suppressionAdded: "suppression.added",
  suppressionRemoved: "suppression.removed",

  campaignStatusChanged: "campaign.status_changed",
  settingChanged: "setting.changed",
  dispatchRun: "dispatch.run",
  workerTick: "worker.tick",
  workerError: "worker.error",
} as const;

export type EventType = (typeof EVENT_TYPES)[keyof typeof EVENT_TYPES];

export interface EventInput {
  type: EventType | string;
  entityType?: string | null;
  entityId?: number | null;
  campaignId?: number | null;
  contactId?: number | null;
  payload?: unknown;
}

export async function recordEvent(input: EventInput): Promise<number> {
  const result = await run(
    `INSERT INTO events (type, entity_type, entity_id, campaign_id, contact_id, payload, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [
      input.type,
      input.entityType ?? null,
      input.entityId ?? null,
      input.campaignId ?? null,
      input.contactId ?? null,
      input.payload === undefined ? null : JSON.stringify(input.payload),
      nowIso(),
    ],
  );
  return result.lastInsertRowid;
}

export async function recentEvents(limit = 100): Promise<OutreachEvent[]> {
  return await all<OutreachEvent>(
    `SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT ?`,
    [limit],
  );
}

export async function eventsForContact(contactId: number, limit = 200): Promise<OutreachEvent[]> {
  return await all<OutreachEvent>(
    `SELECT * FROM events WHERE contact_id = ? ORDER BY created_at DESC, id DESC LIMIT ?`,
    [contactId, limit],
  );
}

export function parsePayload<T = Record<string, unknown>>(event: OutreachEvent): T | null {
  if (!event.payload) return null;
  try {
    return JSON.parse(event.payload) as T;
  } catch {
    return null;
  }
}
