/**
 * Putting contacts into a campaign.
 *
 * Enrollment is where eligibility is decided, and it is deliberately strict:
 * it is far cheaper to hold a contact back for a human to look at than to
 * send a broken or unwanted email and burn the relationship with a company
 * you wanted as a design partner.
 */
import { all, get, run, transaction } from "../db";
import { nowIso, nextWindowSlot } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import { checkSuppressed } from "../core/suppression";
import { getBoolSetting, getNumberSetting, SETTING_KEYS } from "../core/settings";
import type { Campaign, Contact, Enrollment } from "../core/types";

export interface EligibilityResult {
  eligible: boolean;
  reasons: string[];
}

interface LatestVerificationRow {
  status: string;
  score: number;
  is_catch_all: number;
  is_role: number;
}

/**
 * Decides whether a contact may be enrolled. Returns every failing reason
 * rather than the first, so the dashboard can show the full picture instead
 * of making the operator fix problems one at a time.
 */
export async function checkEligibility(contact: Contact): Promise<EligibilityResult> {
  const reasons: string[] = [];

  // YC companies are deliberately absent here: enrolling one is a decision,
  // not an accident, so it is allowed when a human ticks the box. What can
  // never happen is the system choosing them on its own — findCandidates
  // excludes is_yc, so auto-fill cannot see them.

  if (!contact.email_normalized) {
    reasons.push("No email address on record");
  }

  if (
    contact.name_quality === "placeholder" &&
    !(await getBoolSetting(SETTING_KEYS.allowPlaceholderNames))
  ) {
    reasons.push(
      `The sheet lists a job title here ("${contact.full_name ?? "unknown"}") rather than a name, so a personalized greeting would read as automated`,
    );
  }

  if (contact.email_normalized) {
    const suppression = await checkSuppressed(contact.email_normalized);
    if (suppression.suppressed) {
      reasons.push(`Suppressed (${suppression.scope}: ${suppression.matchedValue}): ${suppression.reason}`);
    }

    const verification = await get<LatestVerificationRow>(
      `SELECT status, score, is_catch_all, is_role FROM latest_verification WHERE contact_id = ?`,
      [contact.id],
    );

    if (!verification) {
      reasons.push("Not verified yet. Run verification first.");
    } else {
      const minScore = await getNumberSetting(SETTING_KEYS.minVerificationScore);
      if (verification.status === "invalid") {
        reasons.push("Verification says this address is undeliverable");
      } else if (verification.status === "unknown") {
        reasons.push("Verification was inconclusive. Re-check before sending.");
      } else if (verification.score < minScore) {
        reasons.push(`Verification score ${verification.score} is below the ${minScore} threshold`);
      }
      if (verification.status === "risky" && !(await getBoolSetting(SETTING_KEYS.allowRiskySends))) {
        reasons.push("Address is flagged risky and risky sends are switched off");
      }
    }
  }

  return { eligible: reasons.length === 0, reasons };
}

export interface EnrollOptions {
  campaignId: number;
  contactIds: number[];
  /** Enroll even if eligibility fails. The enrollment is parked in `review`, never sent. */
  force?: boolean;
}

export interface EnrollResult {
  enrolled: number;
  skipped: { contactId: number; company: string; reasons: string[] }[];
  alreadyEnrolled: number;
}

export async function enrollContacts(options: EnrollOptions): Promise<EnrollResult> {
  const campaign = await get<Campaign>(`SELECT * FROM campaigns WHERE id = ?`, [options.campaignId]);
  if (!campaign) throw new Error(`Campaign ${options.campaignId} not found`);

  const firstStep = await get<{ id: number }>(
    `SELECT id FROM sequence_steps WHERE campaign_id = ? AND step_number = 1`,
    [campaign.id],
  );
  if (!firstStep) {
    throw new Error(`Campaign "${campaign.name}" has no step 1. Add a template to the sequence first.`);
  }

  const result: EnrollResult = { enrolled: 0, skipped: [], alreadyEnrolled: 0 };

  await transaction(async (client) => {
    for (const contactId of options.contactIds) {
      const contact = await get<Contact>(`SELECT * FROM contacts WHERE id = ?`, [contactId], client);
      if (!contact) continue;

      const existing = await get<Enrollment>(
        `SELECT * FROM enrollments WHERE campaign_id = ? AND contact_id = ?`,
        [campaign.id, contactId], client);
      if (existing) {
        result.alreadyEnrolled++;
        continue;
      }

      const eligibility = await checkEligibility(contact);
      if (!eligibility.eligible && !options.force) {
        result.skipped.push({
          contactId,
          company: contact.company,
          reasons: eligibility.reasons,
        });
        continue;
      }

      // The first send is scheduled into the campaign's window rather than
      // fired immediately, so an enrollment at 11pm does not produce a batch
      // of emails timestamped 11pm.
      const firstSend = nextWindowSlot(new Date(), campaign, 45);
      const status = eligibility.eligible ? "pending" : "review";

      const inserted = await run(
        `INSERT INTO enrollments (campaign_id, contact_id, status, current_step, next_send_at, enrolled_at, updated_at)
         VALUES (?, ?, ?, 0, ?, ?, ?)`,
        [
          campaign.id,
          contactId,
          status,
          // A forced-but-ineligible enrollment must never be picked up by the
          // scheduler; leaving next_send_at NULL is what guarantees that.
          eligibility.eligible ? firstSend.toISOString() : null,
          nowIso(),
          nowIso(),
        ], client);

      await recordEvent({
        type: EVENT_TYPES.enrollmentCreated,
        entityType: "enrollment",
        entityId: inserted.lastInsertRowid,
        campaignId: campaign.id,
        contactId,
        payload: {
          status,
          scheduled_for: eligibility.eligible ? firstSend.toISOString() : null,
          held_reasons: eligibility.eligible ? [] : eligibility.reasons,
        },
      });

      result.enrolled++;
    }
  });

  return result;
}

export interface CandidateFilter {
  sheet?: string;
  roleGroup?: string;
  industry?: string;
  tier?: string;
  minScore?: number;
  excludeEnrolled?: boolean;
  limit?: number;
}

/** Contacts matching a filter, used by the dashboard's bulk-enroll flow. */
export async function findCandidates(filter: CandidateFilter = {}): Promise<Contact[]> {
  // Two structural exclusions before any filter: no candidate without an
  // address (every address in the DB is Apollo-verified by construction since
  // the guessed-email purge), and no YC company ever.
  const where: string[] = ["c.email_normalized IS NOT NULL", "c.is_yc = 0"];
  const params: unknown[] = [];

  if (filter.sheet) {
    where.push("c.source_sheet = ?");
    params.push(filter.sheet);
  }
  if (filter.roleGroup) {
    where.push("c.role_group = ?");
    params.push(filter.roleGroup);
  }
  if (filter.industry) {
    where.push("c.industry = ?");
    params.push(filter.industry);
  }
  if (filter.tier) {
    where.push("c.tier = ?");
    params.push(filter.tier);
  }
  if (filter.minScore !== undefined) {
    where.push("v.score >= ?");
    params.push(filter.minScore);
  }
  if (filter.excludeEnrolled) {
    where.push("NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.contact_id = c.id)");
  }

  // Candidates spread across companies rather than clustering: the sender
  // enforces one person per company per day, so an auto-fill that grabs four
  // people at the same firm burns three of its picks on contacts that cannot
  // send until later days. First one person from each company (best
  // verification score first), then seconds, and companies already emailed
  // today sort behind fresh ones so a top-up right now is actually sendable.
  const sql = `
    SELECT * FROM (
      SELECT c.*,
        ROW_NUMBER() OVER (
          PARTITION BY c.company ORDER BY COALESCE(v.score, 0) DESC, c.id
        ) AS company_rank,
        EXISTS (
          SELECT 1 FROM messages m JOIN contacts c2 ON c2.id = m.contact_id
          WHERE c2.company = c.company AND m.direction = 'outbound'
            AND m.status IN ('sent', 'bounced') AND DATE(m.sent_at) = DATE('now')
        ) AS contacted_today
      FROM contacts c
      LEFT JOIN latest_verification v ON v.contact_id = c.id
      WHERE ${where.join(" AND ")}
    )
    ORDER BY contacted_today, company_rank, company
    ${filter.limit ? "LIMIT ?" : ""}`;

  if (filter.limit) params.push(filter.limit);
  return await all<Contact>(sql, params);
}

/**
 * Give a scheduled time to any enrollment that has none.
 *
 * Manual mode does not need one, so enrollments can legitimately exist without
 * a slot. Automatic mode does, and an enrollment with next_send_at NULL is
 * invisible to the scheduler: it shows as pending on the dashboard and never
 * sends. Called when switching to automatic so that state cannot survive the
 * switch.
 *
 * Returns how many were given a slot.
 */
export async function scheduleUnscheduledEnrollments(now = new Date()): Promise<number> {
  const rows = await all<{ id: number; campaign_id: number }>(
    `SELECT e.id, e.campaign_id
     FROM enrollments e
     JOIN campaigns c ON c.id = e.campaign_id
     WHERE e.next_send_at IS NULL
       AND e.status IN ('pending', 'active')
       AND c.status <> 'archived'`,
  );

  let scheduled = 0;
  for (const row of rows) {
    const campaign = await get<Campaign>(`SELECT * FROM campaigns WHERE id = ?`, [row.campaign_id]);
    if (!campaign) continue;

    const slot = nextWindowSlot(now, campaign, 45);
    await run(`UPDATE enrollments SET next_send_at = ?, updated_at = ? WHERE id = ?`, [
      slot.toISOString(),
      nowIso(),
      row.id,
    ]);
    await recordEvent({
      type: EVENT_TYPES.enrollmentCreated,
      entityType: "enrollment",
      entityId: row.id,
      campaignId: campaign.id,
      payload: { rescheduled_for: slot.toISOString(), reason: "had no scheduled time" },
    });
    scheduled++;
  }
  return scheduled;
}
