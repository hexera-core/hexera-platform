/**
 * Sending one step of one enrollment.
 *
 * The order of checks below is the safety design, not incidental. Everything
 * that could stop a send is evaluated *before* the message is handed to
 * Gmail, and the suppression check is repeated here even though enrollment
 * already ran it — state can change between enrollment and send, and this is
 * the last point at which we can still decline.
 */
import { get, run, transaction } from "../db";
import { addSendingDays, nowIso } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import { checkSuppressed } from "../core/suppression";
import { getSetting, isDryRun, SETTING_KEYS } from "../core/settings";
import { renderEmail, buildMergeValues, RenderError } from "../mail/render";
import { sendEmail } from "../mail/gmail";
import type { MessageParts } from "../mail/mime";
import type { Campaign, Contact, Enrollment, SequenceStep, Template } from "../core/types";
import { transition, stopSequence } from "./state";
import { checkCapacity, recordSend, type DueItem } from "./scheduler";

export type SendOutcome =
  | { kind: "sent"; messageId: number; gmailMessageId: string; subject: string }
  | { kind: "preview"; messageId: number; subject: string; body: string; to: string }
  | { kind: "skipped"; reason: string }
  | { kind: "stopped"; reason: string }
  | { kind: "completed" }
  | { kind: "failed"; error: string };

interface StepWithTemplate extends SequenceStep {
  template: Template;
}

async function loadStep(campaignId: number, stepNumber: number): Promise<StepWithTemplate | null> {
  const step = await get<SequenceStep>(
    `SELECT * FROM sequence_steps WHERE campaign_id = ? AND step_number = ?`,
    [campaignId, stepNumber],
  );
  if (!step) return null;
  const template = await get<Template>(`SELECT * FROM templates WHERE id = ?`, [step.template_id]);
  if (!template) return null;
  return { ...step, template };
}

/** Has this contact ever replied? The gate on every conditional follow-up. */
async function hasInboundReply(enrollmentId: number): Promise<boolean> {
  const row = await get<{ n: number }>(
    `SELECT COUNT(*) n FROM replies r
     WHERE r.enrollment_id = ?
       AND r.classification NOT IN ('ooo', 'auto', 'bounce')`,
    [enrollmentId],
  );
  return (row?.n ?? 0) > 0;
}

/** The opener's subject — follow-ups reply into that thread rather than starting a new one. */
async function threadSubject(enrollmentId: number): Promise<string | null> {
  const first = await get<{ subject: string | null }>(
    `SELECT subject FROM messages
     WHERE enrollment_id = ? AND direction = 'outbound' AND subject IS NOT NULL
     ORDER BY step_number ASC LIMIT 1`,
    [enrollmentId],
  );
  return first?.subject ?? null;
}

export interface SendStepOptions {
  now?: Date;
  /** Render and evaluate everything, then stop short of sending. Used by the queue preview. */
  previewOnly?: boolean;
  /** Skip the campaign's send window. Set only for a send a person triggered
   *  by hand: the window exists to stop the machine mailing people at odd
   *  hours unattended, not to overrule someone who is standing right there. */
  ignoreWindow?: boolean;
}

export async function sendStep(item: DueItem, options: SendStepOptions = {}): Promise<SendOutcome> {
  const now = options.now ?? new Date();
  const { enrollment, contact, campaign } = item;
  const nextStepNumber = enrollment.current_step + 1;

  // ── 0. Never send the same step twice ───────────────────────────────────
  // Concurrent dispatch runs once raced over one ready list and 22 people
  // received the opener two or three times inside a second. The dispatch
  // mutex stops same-process overlap; this check is the backstop that holds
  // even across processes, because it asks the ledger rather than memory.
  const priorSend = await get<{ id: number }>(
    `SELECT id FROM messages
     WHERE enrollment_id = ? AND step_number = ? AND direction = 'outbound'
       AND status IN ('sent', 'bounced')`,
    [enrollment.id, nextStepNumber],
  );
  if (priorSend) {
    return { kind: "skipped", reason: `step ${nextStepNumber} was already sent (message ${priorSend.id})` };
  }

  // ── 1. Is there another step? ───────────────────────────────────────────
  const step = await loadStep(campaign.id, nextStepNumber);
  if (!step) {
    transition(enrollment.id, "completed", {
      reason: "all sequence steps sent, no reply",
      nextSendAt: null,
    });
    recordEvent({
      type: EVENT_TYPES.enrollmentCompleted,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { steps_sent: enrollment.current_step },
    });
    return { kind: "completed" };
  }

  // ── 2. Follow-ups only when there has been silence ──────────────────────
  if (step.only_if_no_reply && nextStepNumber > 1 && await hasInboundReply(enrollment.id)) {
    stopSequence(enrollment.id, "replied", "contact replied — follow-ups cancelled");
    return { kind: "stopped", reason: "contact already replied" };
  }

  // ── 3. Suppression, re-checked at the last possible moment ──────────────
  const suppression = await checkSuppressed(contact.email_normalized);
  if (suppression.suppressed) {
    stopSequence(
      enrollment.id,
      "suppressed",
      `suppressed (${suppression.scope}: ${suppression.matchedValue}) — ${suppression.reason}`,
    );
    return { kind: "stopped", reason: `suppressed: ${suppression.reason}` };
  }

  if (!contact.email_normalized) {
    stopSequence(enrollment.id, "review", "contact has no email address");
    return { kind: "stopped", reason: "no email address" };
  }

  // ── 4. Capacity and send window ─────────────────────────────────────────
  const domain = contact.domain ?? contact.email_normalized.split("@")[1];
  const capacity = checkCapacity(campaign, domain, now, { ignoreWindow: options.ignoreWindow });
  if (!(await capacity).allowed && !options.previewOnly) {
    return { kind: "skipped", reason: (await capacity).reason ?? "no capacity" };
  }

  // ── 5. Render ───────────────────────────────────────────────────────────
  const isFollowUp = step.template.kind === "follow_up" || nextStepNumber > 1;
  const inheritedSubject = isFollowUp ? await threadSubject(enrollment.id) : null;

  let subject: string;
  let body: string;
  try {
    const values = buildMergeValues(contact, {
      signature: await getSetting(SETTING_KEYS.signature),
      unsubscribe: await getSetting(SETTING_KEYS.unsubscribeLine),
    });
    const rendered = renderEmail(step.template.subject, step.template.body, values);
    body = rendered.body;
    // Gmail threads on subject as well as threadId, so a follow-up must carry
    // the original subject rather than the template's own.
    subject = inheritedSubject
      ? (await inheritedSubject).startsWith("Re: ")
        ? inheritedSubject
        : `Re: ${inheritedSubject}`
      : rendered.subject;
  } catch (error) {
    const message = error instanceof RenderError ? error.message : String(error);
    // A template that cannot render is an authoring bug affecting every
    // contact — park this one for a human rather than retrying forever.
    stopSequence(enrollment.id, "review", `template render failed: ${message}`);
    recordEvent({
      type: EVENT_TYPES.messageFailed,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { error: message, step: nextStepNumber },
    });
    return { kind: "failed", error: message };
  }

  const fromEmail = await getSetting(SETTING_KEYS.sendingAddress);
  if (!fromEmail && !options.previewOnly) {
    return { kind: "skipped", reason: "no sending address configured — run npm run gmail:auth" };
  }

  const parts: MessageParts = {
    from: fromEmail || "unconfigured@localhost",
    fromName: await getSetting(SETTING_KEYS.sendingName),
    to: contact.email_normalized,
    toName: contact.full_name ?? undefined,
    subject,
    body,
    inReplyTo: enrollment.last_message_id,
    references: enrollment.last_message_id ? [enrollment.last_message_id] : undefined,
  };

  if (options.previewOnly) {
    return { kind: "preview", messageId: 0, subject, body, to: contact.email_normalized };
  }

  // ── 6. Send, or record the dry run ──────────────────────────────────────
  const dryRun = await isDryRun();

  if (dryRun) {
    // A dry run is a rehearsal and leaves NOTHING behind: no message row, no
    // step advance, no daily-cap slot. The earlier version recorded a message
    // and advanced the sequence, which meant a contact who had been dry-run
    // through step 1 would start at step 2 the moment sending went live and
    // would never receive the opener at all. Rendering the message is the
    // whole point; persisting it was the bug.
    recordEvent({
      type: EVENT_TYPES.messageDryRun,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { step: nextStepNumber, subject, to: contact.email_normalized },
    });
    return { kind: "preview", messageId: 0, subject, body, to: contact.email_normalized };
  }

  try {
    const result = await sendEmail(parts, { threadId: enrollment.thread_id });

    const messageId = await transaction(async (client) => {
      const inserted = await run(
        `INSERT INTO messages (
           enrollment_id, contact_id, direction, step_number, template_id, status,
           subject, body, snippet, to_email, from_email, gmail_message_id,
           gmail_thread_id, rfc822_message_id, in_reply_to, scheduled_for, sent_at, created_at
         ) VALUES (?, ?, 'outbound', ?, ?, 'sent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          enrollment.id, contact.id, nextStepNumber, step.template_id,
          subject, body, body.slice(0, 200), contact.email_normalized, fromEmail,
          result.gmailMessageId, result.gmailThreadId, result.rfc822MessageId,
          enrollment.last_message_id, enrollment.next_send_at, nowIso(), nowIso(),
        ], client);
      recordEvent({
        type: EVENT_TYPES.messageSent,
        entityType: "message",
        entityId: inserted.lastInsertRowid,
        campaignId: campaign.id,
        contactId: contact.id,
        payload: { step: nextStepNumber, subject, to: contact.email_normalized, thread: result.gmailThreadId },
      });
      return inserted.lastInsertRowid;
    });

    advanceEnrollment(enrollment, campaign, nextStepNumber, result.gmailThreadId, result.rfc822MessageId, now);
    recordSend(campaign, domain, now);

    return { kind: "sent", messageId, gmailMessageId: result.gmailMessageId, subject };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await run(
      `INSERT INTO messages (
         enrollment_id, contact_id, direction, step_number, template_id, status,
         subject, body, to_email, from_email, error, created_at
       ) VALUES (?, ?, 'outbound', ?, ?, 'failed', ?, ?, ?, ?, ?, ?)`,
      [
        enrollment.id, contact.id, nextStepNumber, step.template_id,
        subject, body, contact.email_normalized, fromEmail, message, nowIso(),
      ],
    );
    recordEvent({
      type: EVENT_TYPES.messageFailed,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { error: message, step: nextStepNumber },
    });

    // Retry once on the next window; a second failure needs a human.
    const alreadyFailed = await get<{ n: number }>(
      `SELECT COUNT(*) n FROM messages WHERE enrollment_id = ? AND status = 'failed' AND step_number = ?`,
      [enrollment.id, nextStepNumber],
    );
    if ((alreadyFailed?.n ?? 0) >= 2) {
      stopSequence(enrollment.id, "review", `send failed twice: ${message}`);
    } else {
      transition(enrollment.id, "active", {
        nextSendAt: addSendingDays(now, 1, campaign, 60).toISOString(),
      });
    }
    return { kind: "failed", error: message };
  }
}

/**
 * Moves the enrollment forward and books the next step.
 *
 * When there is no next step the enrollment stays `active` with a null
 * next_send_at rather than being marked complete immediately — the contact
 * still has the follow-up window to reply, and closing them out now would
 * misreport the campaign's true outcome.
 */
async function advanceEnrollment(
  enrollment: Enrollment,
  campaign: Campaign,
  sentStep: number,
  threadId: string | null,
  rfc822MessageId: string | null,
  now: Date,
): Promise<void> {
  const nextStep = await get<SequenceStep>(
    `SELECT * FROM sequence_steps WHERE campaign_id = ? AND step_number = ?`,
    [campaign.id, sentStep + 1],
  );

  const nextSendAt = nextStep
    ? addSendingDays(now, Math.max(1, nextStep.delay_days), campaign, 90).toISOString()
    : null;

  transition(enrollment.id, "active", {
    currentStep: sentStep,
    nextSendAt,
    threadId: threadId ?? enrollment.thread_id,
    lastMessageId: rfc822MessageId ?? enrollment.last_message_id,
  });

  recordEvent({
    type: EVENT_TYPES.enrollmentStepAdvanced,
    entityType: "enrollment",
    entityId: enrollment.id,
    campaignId: campaign.id,
    contactId: enrollment.contact_id,
    payload: { step: sentStep, next_step_at: nextSendAt },
  });

  if (!nextStep) {
    // Sequence exhausted. Give the contact the same grace period the last gap
    // used before declaring the campaign finished for them.
    await run(`UPDATE enrollments SET next_send_at = ? WHERE id = ?`, [
      addSendingDays(now, 5, campaign, 0).toISOString(),
      enrollment.id,
    ]);
  }
}

/** Renders what a step would look like, with no side effects. Powers the preview pane. */
export async function previewStep(item: DueItem): Promise<SendOutcome> {
  return sendStep(item, { previewOnly: true });
}

export interface ContactPreview {
  subject: string;
  body: string;
  to: string;
  error: string | null;
}

export async function previewTemplateForContact(
  template: Template,
  contact: Contact,
): Promise<ContactPreview> {
  try {
    const rendered = renderEmail(
      template.subject,
      template.body,
      buildMergeValues(contact, {
        signature: await getSetting(SETTING_KEYS.signature),
        unsubscribe: await getSetting(SETTING_KEYS.unsubscribeLine),
      }),
    );
    return {
      subject: rendered.subject,
      body: rendered.body,
      to: contact.email_normalized ?? "(no address)",
      error: null,
    };
  } catch (error) {
    return {
      subject: template.subject,
      body: template.body,
      to: contact.email_normalized ?? "(no address)",
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
