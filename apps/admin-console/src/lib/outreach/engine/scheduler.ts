/**
 * Deciding what may be sent, right now.
 *
 * The scheduler answers one question — "which enrollments are due, and is
 * there capacity for them" — and it is intentionally conservative. Volume is
 * the thing that gets a domain flagged as a spammer, and unlike a bad
 * template, a reputation problem is slow and expensive to undo.
 *
 * Three independent caps apply, smallest wins:
 *   global    — across every campaign, the mailbox's total for the day
 *   campaign  — this campaign's own budget
 *   domain    — how many people at one company hear from us in a day
 *
 * The domain cap is the one people forget. Four contacts at Hermeus receiving
 * the same note on the same morning reads as a blast; spread across four days
 * it reads as persistence.
 */
import { all, get, run } from "../db";
import { dayKey, isWithinSendWindow, nowIso } from "../core/time";
import type { Campaign, Contact, Enrollment } from "../core/types";

export interface DueItem {
  enrollment: Enrollment;
  contact: Contact;
  campaign: Campaign;
}

/**
 * Everything that COULD be sent right now, in manual mode.
 *
 * First emails ignore the clock: if a person just enrolled someone and wants
 * to press send, making them wait for a timer they did not set is theatre.
 * Follow-ups are different — once a step has gone out, the enrollment leaves
 * this list and only returns when its follow-up delay has actually elapsed.
 * Showing a freshly-emailed contact as "ready" again would invite sending
 * the follow-up the same afternoon. The per-contact guards that matter
 * (suppression, replies, missing name, caps) all live in sendStep and
 * still apply.
 */
export async function readyEnrollments(limit = 500, now = new Date()): Promise<DueItem[]> {
  return hydrate(
    await all<Record<string, unknown>>(
      `SELECT ${ENROLLMENT_COLUMNS}
       FROM enrollments e
       JOIN campaigns c ON c.id = e.campaign_id
       WHERE e.status IN ('pending', 'active')
         AND c.status <> 'archived'
         AND (e.current_step = 0 OR e.next_send_at IS NULL OR e.next_send_at <= ?)
       ORDER BY e.id ASC
       LIMIT ?`,
      [now.toISOString(), limit],
    ),
  );
}

/** Look up specific enrollments, for "send exactly these". */
export async function enrollmentsByIds(ids: number[]): Promise<DueItem[]> {
  if (!ids.length) return [];
  const placeholders = ids.map(() => "?").join(", ");
  return hydrate(
    await all<Record<string, unknown>>(
      `SELECT ${ENROLLMENT_COLUMNS}
       FROM enrollments e
       JOIN campaigns c ON c.id = e.campaign_id
       WHERE e.id IN (${placeholders})
         AND e.status IN ('pending', 'active')
         AND c.status <> 'archived'`,
      ids,
    ),
  );
}

const ENROLLMENT_COLUMNS = `
  e.id AS e_id, e.campaign_id, e.contact_id, e.status AS e_status, e.current_step,
  e.next_send_at, e.thread_id, e.last_message_id, e.stopped_reason, e.enrolled_at,
  e.updated_at AS e_updated_at`;

async function hydrate(rows: Record<string, unknown>[]): Promise<DueItem[]> {
  const items: DueItem[] = [];
  for (const row of rows) {
    const campaign = await get<Campaign>(`SELECT * FROM campaigns WHERE id = ?`, [row.campaign_id as number]);
    const contact = await get<Contact>(`SELECT * FROM contacts WHERE id = ?`, [row.contact_id as number]);
    if (!campaign || !contact) continue;

    items.push({
      campaign,
      contact,
      enrollment: {
        id: row.e_id as number,
        campaign_id: row.campaign_id as number,
        contact_id: row.contact_id as number,
        status: row.e_status as Enrollment["status"],
        current_step: row.current_step as number,
        next_send_at: row.next_send_at as string | null,
        thread_id: row.thread_id as string | null,
        last_message_id: row.last_message_id as string | null,
        stopped_reason: row.stopped_reason as string | null,
        enrolled_at: row.enrolled_at as string,
        updated_at: row.e_updated_at as string,
      },
    });
  }
  return items;
}

export async function dueEnrollments(now = new Date(), limit = 200): Promise<DueItem[]> {
  const rows = all<Record<string, unknown>>(
    `SELECT
       e.id AS e_id, e.campaign_id, e.contact_id, e.status AS e_status, e.current_step,
       e.next_send_at, e.thread_id, e.last_message_id, e.stopped_reason, e.enrolled_at,
       e.updated_at AS e_updated_at
     FROM enrollments e
     JOIN campaigns c ON c.id = e.campaign_id
     WHERE e.status IN ('pending', 'active')
       AND e.next_send_at IS NOT NULL
       AND e.next_send_at <= ?
       AND c.status <> 'archived'
     ORDER BY e.next_send_at ASC
     LIMIT ?`,
    [now.toISOString(), limit],
  );
  return hydrate(await rows);
}

// ── Capacity ──────────────────────────────────────────────────────────────

export interface CapacityVerdict {
  allowed: boolean;
  reason: string | null;
  counts: { campaign: number; domain: number };
  limits: { campaign: number; domain: number };
}

async function countsFor(day: string, campaignId: number, domain: string) {
  const campaign =
    (await get<{ n: number }>(
      `SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ? AND campaign_id = ?`,
      [day, campaignId],
    ))?.n ?? 0;
  const domainCount =
    (await get<{ n: number }>(
      `SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ? AND domain = ?`,
      [day, domain],
    ))?.n ?? 0;
  return { campaign, domain: domainCount };
}

export interface CapacityOptions {
  /** Manual sends skip the send window. It exists to stop the machine mailing
   *  people at 3am unattended; when a person presses the button, the person is
   *  the one choosing the hour, and overriding that would be paternalistic.
   *  The volume caps still apply, because those protect the sending domain
   *  rather than the recipient's evening. */
  ignoreWindow?: boolean;
}

export async function checkCapacity(
  campaign: Campaign,
  domain: string,
  now = new Date(),
  options: CapacityOptions = {},
): Promise<CapacityVerdict> {
  const day = dayKey(now, campaign.timezone);
  const counts = await countsFor(day, campaign.id, domain);
  // The campaign's own cap is the single volume lever, by explicit choice:
  // a separate global cap on top of it meant two numbers to keep in sync
  // and confusing skips when they disagreed.
  const limits = {
    campaign: campaign.daily_cap,
    domain: campaign.per_domain_daily_cap,
  };

  if (!options.ignoreWindow && !isWithinSendWindow(now, campaign)) {
    return { allowed: false, reason: "outside the campaign's send window", counts, limits };
  }
  if (counts.campaign >= limits.campaign) {
    return {
      allowed: false,
      reason: `campaign daily cap reached (${counts.campaign}/${limits.campaign})`,
      counts,
      limits,
    };
  }
  if (counts.domain >= limits.domain) {
    return {
      allowed: false,
      reason: `already contacted ${counts.domain} person(s) at ${domain} today (limit ${limits.domain})`,
      counts,
      limits,
    };
  }
  return { allowed: true, reason: null, counts, limits };
}

/**
 * Records one send against the day's budget.
 *
 * Dry runs count too. The point of a dry run is to model the real day
 * faithfully — pacing included — so the queue you preview is the queue you
 * would actually get.
 */
export async function recordSend(campaign: Campaign, domain: string, now = new Date()): Promise<void> {
  const day = dayKey(now, campaign.timezone);
  await run(
    `INSERT INTO send_ledger (day, campaign_id, domain, count) VALUES (?, ?, ?, 1)
     ON CONFLICT (day, campaign_id, domain) DO UPDATE SET count = count + 1`,
    [day, campaign.id, domain],
  );
}

export interface CapacitySnapshot {
  day: string;
  /** Everything sent today across all campaigns; informational, not a limit. */
  globalUsed: number;
  perCampaign: { campaignId: number; name: string; used: number; limit: number }[];
}

/** Today's budget usage, for the dashboard header. */
export async function capacitySnapshot(now = new Date()): Promise<CapacitySnapshot> {
  const campaigns = await all<Campaign>(`SELECT * FROM campaigns WHERE status <> 'archived'`);
  const day = dayKey(now, campaigns[0]?.timezone ?? "UTC");

  return {
    day,
    globalUsed: (await get<{ n: number }>(`SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ?`, [day]))?.n ?? 0,
    // Promise.all, because the per-campaign counter is a query: a plain .map would build an array
    // of promises and the caller would render "[object Promise] of 40".
    perCampaign: await Promise.all(campaigns.map(async (campaign) => ({
      campaignId: campaign.id,
      name: campaign.name,
      used:
        (await get<{ n: number }>(
          `SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ? AND campaign_id = ?`,
          [dayKey(now, campaign.timezone), campaign.id],
        ))?.n ?? 0,
      limit: campaign.daily_cap,
    }))),
  };
}

/** The next N scheduled sends, for the queue preview. */
export async function upcomingQueue(limit = 100): Promise<{
  enrollment_id: number;
  /** Null while an enrollment sits in review and nothing is scheduled. */
  next_send_at: string | null;
  step: number;
  company: string;
  full_name: string | null;
  email: string | null;
  campaign_name: string;
  status: string;
}[]> {
  return await all(
    `SELECT
       e.id AS enrollment_id, e.next_send_at, e.current_step + 1 AS step, e.status,
       c.company, c.full_name, c.email_normalized AS email, ca.name AS campaign_name
     FROM enrollments e
     JOIN contacts c ON c.id = e.contact_id
     JOIN campaigns ca ON ca.id = e.campaign_id
     WHERE e.status IN ('pending', 'active', 'review')
     ORDER BY e.next_send_at IS NULL, e.next_send_at ASC
     LIMIT ?`,
    [limit],
  );
}

export async function markSkipped(enrollmentId: number, reason: string): Promise<void> {
  // Only touches updated_at: a skip is not a state change, just a note that
  // this pass declined to act. The enrollment stays due for the next tick.
  await run(`UPDATE enrollments SET updated_at = ? WHERE id = ?`, [nowIso(), enrollmentId]);
  void reason;
}
