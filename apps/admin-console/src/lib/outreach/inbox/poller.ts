/**
 * Inbound reply processing.
 *
 * Pulls recent inbox messages from Gmail, matches them to enrollments,
 * classifies them, and applies the consequences. This is the half of the
 * system that makes the other half safe to run: without it, follow-ups would
 * keep firing at people who have already answered.
 *
 * Matching is by Gmail thread first, sender address second. Thread matching is
 * exact; the address fallback catches the case where someone replies from a
 * different client that broke the thread, or forwards to a colleague who
 * writes back fresh.
 */
import { all, get, run, transaction } from "../db";
import { nowIso, addSendingDays } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import { suppress, domainOf } from "../core/suppression";
import { getBoolSetting, SETTING_KEYS } from "../core/settings";
import { listInboundMessages, fetchMessage, starMessage, type FetchedMessage } from "../mail/gmail";
import { parseInboundHeaders, stripQuotedText } from "../mail/mime";
import { classifyReply, type ClassificationResult } from "./classify";
import { classifyWithLlm, isLlmClassifierAvailable } from "./classify-llm";
import { stopSequence, transition } from "../engine/state";
import type { Campaign, Contact, Enrollment } from "../core/types";

export interface PollResult {
  scanned: number;
  matched: number;
  newReplies: number;
  byClassification: Record<string, number>;
  stopped: number;
  suppressed: number;
  errors: string[];
}

async function alreadyProcessed(gmailMessageId: string): Promise<boolean> {
  return Boolean(
    await get<{ id: number }>(`SELECT id FROM messages WHERE gmail_message_id = ?`, [gmailMessageId]),
  );
}

interface MatchedEnrollment {
  enrollment: Enrollment;
  contact: Contact;
  campaign: Campaign;
}

const BOUNCE_SENDER = /^(mailer-daemon|postmaster)@/i;

/**
 * A bounce report is written by the mail system, not the recipient, so the
 * address that identifies the contact is inside the report rather than on
 * the From line. Google's own bounces sometimes open a fresh thread, which
 * defeats thread matching too — that exact combination silently dropped a
 * real bounce (caleb.dada@eridan.io) while the in-thread kind was caught.
 */
async function failedRecipient(message: FetchedMessage): Promise<string | null> {
  const header = message.headers.find((h) => h.name?.toLowerCase() === "x-failed-recipients")?.value;
  if (header?.trim()) return header.trim().toLowerCase();
  const text = `${message.snippet}\n${message.body}`;
  const stated = text.match(
    /(?:wasn't delivered to|could not be delivered to|delivery to|failed for|final-recipient:[^;]*;)\s*<?([^\s<>;,"']+@[^\s<>;,"']+)>?/i,
  );
  return stated ? stated[1].toLowerCase().replace(/\.+$/, "") : null;
}

async function matchEnrollment(message: FetchedMessage, fromEmail: string): Promise<MatchedEnrollment | null> {
  let enrollment = await get<Enrollment>(
    `SELECT * FROM enrollments WHERE thread_id = ? ORDER BY updated_at DESC LIMIT 1`,
    [message.threadId],
  );

  if (!enrollment) {
    // Thread broken or reply came from elsewhere — fall back to an address,
    // preferring the most recently touched live enrollment. For a bounce
    // report, the useful address is the one the mail system says failed;
    // the From line is just the robot that wrote the report.
    const address = BOUNCE_SENDER.test(fromEmail)
      ? ((await failedRecipient(message)) ?? fromEmail)
      : fromEmail;
    enrollment = await get<Enrollment>(
      `SELECT e.* FROM enrollments e
       JOIN contacts c ON c.id = e.contact_id
       WHERE c.email_normalized = ?
       ORDER BY CASE WHEN e.status IN ('pending','active') THEN 0 ELSE 1 END, e.updated_at DESC
       LIMIT 1`,
      [address],
    );
  }
  if (!enrollment) return null;

  const contact = await get<Contact>(`SELECT * FROM contacts WHERE id = ?`, [enrollment.contact_id]);
  const campaign = await get<Campaign>(`SELECT * FROM campaigns WHERE id = ?`, [enrollment.campaign_id]);
  if (!contact || !campaign) return null;

  return { enrollment, contact, campaign };
}

/**
 * Turns a classification into state changes.
 *
 * Every branch either halts the sequence or explicitly decides not to. There
 * is no path where a human reply is recorded and the sequence simply carries
 * on unexamined.
 */
async function applyOutcome(
  match: MatchedEnrollment,
  verdict: ClassificationResult,
  result: PollResult,
): Promise<void> {
  const { enrollment, contact, campaign } = match;

  switch (verdict.classification) {
    case "negative": {
      if (await getBoolSetting(SETTING_KEYS.autoStopOnNegative)) {
        await stopSequence(enrollment.id, "stopped", `negative reply (${verdict.matchedRules.join(", ")})`, "reply-classifier");
        result.stopped++;
      }
      if (contact.email_normalized) {
        // Scope matters: "not interested" silences this person, "unsubscribe"
        // silences the company.
        const scope =
          verdict.optOutScope === "domain" && await getBoolSetting(SETTING_KEYS.autoSuppressDomainOnOptOut)
            ? "domain"
            : "email";
        const value = scope === "domain" ? domainOf(contact.email_normalized) : contact.email_normalized;
        if (
          await suppress({
            scope,
            value,
            reason: verdict.optOutScope === "domain" ? "explicit opt-out request" : "declined outreach",
            source: "negative_reply",
            contactId: contact.id,
          })
        ) {
          result.suppressed++;
        }
      }
      break;
    }

    case "positive": {
      // Not "stopped" — this is the outcome the campaign exists to produce.
      // A human takes it from here.
      await stopSequence(enrollment.id, "replied", "positive reply — handed to a human", "reply-classifier");
      result.stopped++;
      break;
    }

    case "neutral": {
      await stopSequence(enrollment.id, "review", "reply needs a human read before continuing", "reply-classifier");
      result.stopped++;
      break;
    }

    case "ooo": {
      // Explicitly NOT a reply. Push the next step out and keep going —
      // cancelling a sequence because someone is on holiday would waste the
      // contact entirely.
      if (enrollment.status === "active" || enrollment.status === "pending") {
        const resumeAt = addSendingDays(new Date(), 7, campaign, 60);
        await transition(enrollment.id, "active", {
          nextSendAt: resumeAt.toISOString(),
          reason: "out-of-office — follow-up deferred",
          actor: "reply-classifier",
        });
      }
      break;
    }

    case "auto":
      // Ticket acknowledgements and no-reply notifications change nothing.
      break;

    case "bounce": {
      await stopSequence(enrollment.id, "bounced", "hard bounce — address is undeliverable", "reply-classifier");
      result.stopped++;

      // Gmail accepted the message, then the receiving server refused it. It
      // was sent but never delivered, and every figure on the dashboard that
      // says "delivered" has to stop counting it. Marking the message itself
      // is what does that: the analytics count status 'sent', so a message
      // moved to 'bounced' drops out of all of them at once rather than
      // needing every query to remember to subtract.
      await run(
        `UPDATE messages SET status = 'bounced', error = ?
         WHERE enrollment_id = ? AND direction = 'outbound' AND status = 'sent'`,
        ["hard bounce, address is undeliverable", enrollment.id],
      );
      if (contact.email_normalized) {
        if (
          await suppress({
            scope: "email",
            value: contact.email_normalized,
            reason: "hard bounce",
            source: "bounce",
            contactId: contact.id,
          })
        ) {
          result.suppressed++;
        }
      }
      break;
    }
  }
}

export interface PollOptions {
  /** Gmail search query. Defaults to the last 30 days of inbox mail. */
  query?: string;
  maxResults?: number;
  /** Consult the Claude classifier when the rules are unsure. */
  useLlm?: boolean;
}

export async function pollReplies(options: PollOptions = {}): Promise<PollResult> {
  const result: PollResult = {
    scanned: 0,
    matched: 0,
    newReplies: 0,
    byClassification: {},
    stopped: 0,
    suppressed: 0,
    errors: [],
  };

  const useLlm = options.useLlm ?? isLlmClassifierAvailable();
  const ids = await listInboundMessages({
    query: options.query ?? "in:inbox newer_than:30d",
    maxResults: options.maxResults ?? 100,
  });

  for (const id of ids) {
    result.scanned++;
    if (await alreadyProcessed(id)) continue;

    try {
      const message = await fetchMessage(id);
      const headers = parseInboundHeaders(message.headers);
      const match = await matchEnrollment(message, headers.from);

      // Unmatched inbox mail is somebody else's conversation — leave it alone.
      if (!match) continue;
      result.matched++;

      const cleanBody = stripQuotedText(message.body || message.snippet);

      let verdict = classifyReply({
        body: cleanBody,
        subject: headers.subject,
        fromEmail: headers.from,
        isAutoSubmitted: headers.isAutoSubmitted,
      });

      // Only escalate the genuinely uncertain cases — the rules are cheap and
      // right most of the time.
      if (useLlm && (verdict.requiresReview || verdict.confidence < 0.7)) {
        verdict = await classifyWithLlm(
          { subject: headers.subject, body: cleanBody, fromEmail: headers.from },
          verdict,
        );
      }

      await transaction(async (client) => {
        const inserted = await run(
          `INSERT INTO messages (
             enrollment_id, contact_id, direction, status, subject, body, snippet,
             to_email, from_email, gmail_message_id, gmail_thread_id,
             rfc822_message_id, in_reply_to, received_at, created_at
           ) VALUES (?, ?, 'inbound', 'received', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            match.enrollment.id,
            match.contact.id,
            headers.subject,
            cleanBody,
            message.snippet.slice(0, 200),
            headers.to,
            headers.from,
            message.id,
            message.threadId,
            headers.messageId,
            headers.inReplyTo,
            headers.date ?? nowIso(),
            nowIso(),
          ], client);

        await run(
          `INSERT INTO replies (
             message_id, enrollment_id, contact_id, classification, confidence,
             classifier, matched_rules, requires_review, received_at, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            inserted.lastInsertRowid,
            match.enrollment.id,
            match.contact.id,
            verdict.classification,
            verdict.confidence,
            verdict.classifier,
            JSON.stringify(verdict.matchedRules),
            verdict.requiresReview ? 1 : 0,
            headers.date ?? nowIso(),
            nowIso(),
          ], client);

        await recordEvent({
          type: EVENT_TYPES.replyClassified,
          entityType: "reply",
          entityId: inserted.lastInsertRowid,
          campaignId: match.campaign.id,
          contactId: match.contact.id,
          payload: {
            classification: verdict.classification,
            confidence: verdict.confidence,
            classifier: verdict.classifier,
            requires_review: verdict.requiresReview,
            snippet: cleanBody.slice(0, 200),
          },
        });
      });

      // A human wrote back: star the conversation in Gmail itself, so the
      // real inbox shows which threads matter without opening the dashboard.
      // Best-effort — a missing gmail.modify grant must never break polling.
      if (["positive", "negative", "neutral"].includes(verdict.classification)) {
        try {
          await starMessage(message.id);
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          result.errors.push(`star failed for ${headers.from}: ${detail}`);
        }
      }

      result.newReplies++;
      result.byClassification[verdict.classification] =
        (result.byClassification[verdict.classification] ?? 0) + 1;

      await applyOutcome(match, verdict, result);
    } catch (error) {
      result.errors.push(`${id}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  return result;
}

/** Replies a human still needs to look at — the triage queue. */
export interface ReplyFilter {
  /** One classification, or undefined for all of them. */
  classification?: string;
  /** Only the ones actually waiting on a person: flagged for review and not
   *  yet signed off. A bounce is unreviewed forever because it never needed
   *  reviewing, and counting it as pending work is how a queue that says 1
   *  turns out to contain nothing you can act on. */
  needsReviewOnly?: boolean;
  limit?: number;
}

/**
 * Replies, filtered.
 *
 * The old version only ever returned unreviewed ones, which quietly hid every
 * reply the system resolved by itself. Bounces are the case that matters:
 * they are auto-resolved by design, so they never needed review, so they
 * never appeared anywhere. A message can be undeliverable and invisible at
 * the same time, which is the worst combination.
 */
export async function listReplies(filter: ReplyFilter = {}) {
  const where: string[] = [];
  const params: unknown[] = [];

  if (filter.needsReviewOnly) where.push("r.requires_review = 1 AND r.reviewed_at IS NULL");
  if (filter.classification) {
    where.push("r.classification = ?");
    params.push(filter.classification);
  }
  params.push(filter.limit ?? 200);

  return await all(
    `SELECT
       r.id, r.classification, r.confidence, r.classifier, r.matched_rules,
       r.received_at, r.requires_review, r.reviewed_at,
       m.subject, m.body, m.snippet, m.to_email,
       c.id AS contact_id, c.company, c.full_name, c.email_normalized AS email,
       c.role, c.priority,
       e.id AS enrollment_id, e.status AS enrollment_status,
       ca.name AS campaign_name
     FROM replies r
     JOIN messages m ON m.id = r.message_id
     LEFT JOIN contacts c ON c.id = r.contact_id
     LEFT JOIN enrollments e ON e.id = r.enrollment_id
     LEFT JOIN campaigns ca ON ca.id = e.campaign_id
     ${where.length ? `WHERE ${where.join(" AND ")}` : ""}
     ORDER BY r.requires_review DESC, r.received_at DESC
     LIMIT ?`,
    params,
  );
}

/** Counts per classification, for the filter chips. */
export async function replyCounts(): Promise<{ classification: string; n: number; unreviewed: number }[]> {
  return await all(
    `SELECT classification,
            COUNT(*) AS n,
            SUM(CASE WHEN requires_review = 1 AND reviewed_at IS NULL THEN 1 ELSE 0 END) AS unreviewed
     FROM replies GROUP BY classification ORDER BY n DESC`,
  );
}

export async function pendingReviewReplies(limit = 100) {
  return await all(
    `SELECT
       r.id, r.classification, r.confidence, r.classifier, r.matched_rules,
       r.received_at, r.requires_review,
       m.subject, m.body, m.snippet,
       c.id AS contact_id, c.company, c.full_name, c.email_normalized AS email,
       c.role, c.priority,
       e.id AS enrollment_id, e.status AS enrollment_status,
       ca.name AS campaign_name
     FROM replies r
     JOIN messages m ON m.id = r.message_id
     LEFT JOIN contacts c ON c.id = r.contact_id
     LEFT JOIN enrollments e ON e.id = r.enrollment_id
     LEFT JOIN campaigns ca ON ca.id = e.campaign_id
     WHERE r.reviewed_at IS NULL
     ORDER BY r.requires_review DESC, r.received_at DESC
     LIMIT ?`,
    [limit],
  );
}
