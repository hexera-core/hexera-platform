/**
 * Every number the dashboard shows.
 *
 * Two conventions hold throughout:
 *
 *  1. Live sends and dry runs are counted and reported SEPARATELY, always.
 *     They are summed for pacing and coverage maths (a dry run does consume a
 *     daily-cap slot, because the point of a dry run is to model the real
 *     day), but no figure labelled "sent" ever includes a dry run. A number
 *     that implies mail went out when it didn't is the single most damaging
 *     thing this dashboard could show.
 *
 *  2. Rates are computed over the right denominator, not the convenient one.
 *     Reply rate is replies per *contact who was actually emailed*, never per
 *     contact in the list — the second number flatters the campaign and tells
 *     you nothing about whether the message works.
 */
import { all, get, scalar } from "../db";
import { lastNDays } from "../core/time";

/** Real mail only. Dry runs persist nothing, so there is one notion of "sent". */
const SENT_STATUSES = `('sent')`;

// ── Overview ──────────────────────────────────────────────────────────────

export interface FunnelStage {
  key: string;
  label: string;
  value: number;
  /** Percentage of the previous stage, or null for the first. */
  ofPrevious: number | null;
  hint: string;
}

export async function funnel(): Promise<FunnelStage[]> {
  const contacts = (await scalar<number>(`SELECT COUNT(*) FROM contacts`)) ?? 0;
  const withEmail = (await scalar<number>(`SELECT COUNT(*) FROM contacts WHERE email_normalized IS NOT NULL`)) ?? 0;
  const verified = (await scalar<number>(`SELECT COUNT(*) FROM latest_verification WHERE status = 'valid'`)) ?? 0;
  // "In flight" means still going: pending or active. Someone you enrolled and
  // then removed is not in flight, and counting them as though they were is
  // how a dashboard starts lying to you. `everEnrolled` is kept only as the
  // denominator for the next stage, since Contacted is a share of everyone who
  // was ever in a sequence rather than of the ones still in one.
  const inFlight =
    (await scalar<number>(
      `SELECT COUNT(DISTINCT contact_id) FROM enrollments WHERE status IN ('pending', 'active')`,
    )) ?? 0;
  const everEnrolled = (await scalar<number>(`SELECT COUNT(DISTINCT contact_id) FROM enrollments`)) ?? 0;
  const contacted =
    (await scalar<number>(
      `SELECT COUNT(DISTINCT contact_id) FROM messages
       WHERE direction = 'outbound' AND status IN ${SENT_STATUSES}`,
    )) ?? 0;
  const replied =
    (await scalar<number>(
      `SELECT COUNT(DISTINCT contact_id) FROM replies
       WHERE classification NOT IN ('ooo', 'auto', 'bounce')`,
    )) ?? 0;
  const positive =
    (await scalar<number>(`SELECT COUNT(DISTINCT contact_id) FROM replies WHERE classification = 'positive'`)) ?? 0;

  const pct = (value: number, previous: number) => (previous > 0 ? (value / previous) * 100 : null);

  return [
    { key: "contacts", label: "In list", value: contacts, ofPrevious: null, hint: "Imported from the spreadsheet" },
    { key: "email", label: "Has address", value: withEmail, ofPrevious: pct(withEmail, contacts), hint: "Rows with an email on file" },
    { key: "verified", label: "Deliverable", value: verified, ofPrevious: pct(verified, withEmail), hint: "Domain accepts mail, no red flags" },
    { key: "inFlight", label: "In flight", value: inFlight, ofPrevious: pct(inFlight, verified), hint: "In a sequence right now" },
    { key: "contacted", label: "Contacted", value: contacted, ofPrevious: pct(contacted, everEnrolled), hint: "At least one message sent" },
    { key: "replied", label: "Replied", value: replied, ofPrevious: pct(replied, contacted), hint: "A human wrote back" },
    { key: "positive", label: "Interested", value: positive, ofPrevious: pct(positive, replied), hint: "Reply read as positive" },
  ];
}

export interface Kpis {
  /** Messages accepted by Gmail. */
  liveSent: number;
  /** People who have received at least one message. */
  liveContactsReached: number;
  /** Same figure, kept as the denominator name the rate maths reads from. */
  contactsReached: number;
  humanReplies: number;
  positiveReplies: number;
  negativeReplies: number;
  bounces: number;
  replyRate: number;
  positiveRate: number;
  bounceRate: number;
  awaitingReview: number;
  suppressed: number;
  activeSequences: number;
}

export async function kpis(): Promise<Kpis> {
  const liveSent =
    (await scalar<number>(
      `SELECT COUNT(*) FROM messages WHERE direction = 'outbound' AND status IN ${SENT_STATUSES}`,
    )) ?? 0;
  const liveContactsReached =
    (await scalar<number>(
      `SELECT COUNT(DISTINCT contact_id) FROM messages
       WHERE direction = 'outbound' AND status IN ${SENT_STATUSES}`,
    )) ?? 0;
  const contactsReached =
    (await scalar<number>(
      `SELECT COUNT(DISTINCT contact_id) FROM messages
       WHERE direction = 'outbound' AND status IN ${SENT_STATUSES}`,
    )) ?? 0;

  const byClass = Object.fromEntries(
    (await all<{ classification: string; n: number }>(
      `SELECT classification, COUNT(*) n FROM replies GROUP BY classification`,
    )).map((r) => [r.classification, r.n]),
  );

  const humanReplies = (byClass.positive ?? 0) + (byClass.negative ?? 0) + (byClass.neutral ?? 0);
  const bounces = byClass.bounce ?? 0;

  return {
    liveSent,
    liveContactsReached,
    contactsReached,
    humanReplies,
    positiveReplies: byClass.positive ?? 0,
    negativeReplies: byClass.negative ?? 0,
    bounces,
    replyRate: contactsReached > 0 ? (humanReplies / contactsReached) * 100 : 0,
    positiveRate: humanReplies > 0 ? ((byClass.positive ?? 0) / humanReplies) * 100 : 0,
    bounceRate: contactsReached > 0 ? (bounces / contactsReached) * 100 : 0,
    awaitingReview: (await scalar<number>(`SELECT COUNT(*) FROM replies WHERE reviewed_at IS NULL AND requires_review = 1`)) ?? 0,
    suppressed: (await scalar<number>(`SELECT COUNT(*) FROM suppressions`)) ?? 0,
    activeSequences: (await scalar<number>(`SELECT COUNT(*) FROM enrollments WHERE status IN ('pending','active')`)) ?? 0,
  };
}

// ── Time series ───────────────────────────────────────────────────────────

export interface DayPoint {
  day: string;
  sent: number;
  replies: number;
  positive: number;
  negative: number;
}

export async function dailyActivity(days = 30): Promise<DayPoint[]> {
  const keys = lastNDays(days, "UTC");
  const since = `${keys[0]}T00:00:00.000Z`;

  const sent = Object.fromEntries(
    (await all<{ day: string; n: number }>(
      `SELECT substr(COALESCE(sent_at, created_at), 1, 10) day, COUNT(*) n
       FROM messages
       WHERE direction = 'outbound' AND status IN ${SENT_STATUSES}
         AND COALESCE(sent_at, created_at) >= ?
       GROUP BY day`,
      [since],
    )).map((r) => [r.day, r.n]),
  );

  const replies = await all<{ day: string; classification: string; n: number }>(
    `SELECT substr(received_at, 1, 10) day, classification, COUNT(*) n
     FROM replies WHERE received_at >= ? GROUP BY day, classification`,
    [since],
  );

  const replyByDay = new Map<string, { total: number; positive: number; negative: number }>();
  for (const row of replies) {
    const entry = replyByDay.get(row.day) ?? { total: 0, positive: 0, negative: 0 };
    // OOO and bounces are not replies for reporting purposes either.
    if (!["ooo", "auto", "bounce"].includes(row.classification)) entry.total += row.n;
    if (row.classification === "positive") entry.positive += row.n;
    if (row.classification === "negative") entry.negative += row.n;
    replyByDay.set(row.day, entry);
  }

  return keys.map((day) => {
    const r = replyByDay.get(day) ?? { total: 0, positive: 0, negative: 0 };
    return {
      day,
      sent: sent[day] ?? 0,
      replies: r.total,
      positive: r.positive,
      negative: r.negative,
    };
  });
}

/** Running total of outbound volume. The shape of the ramp matters more than
 *  any single day when warming a new sending address. */
export async function cumulativeSends(days = 60): Promise<{ day: string; sent: number }[]> {
  let sent = 0;
  return (await dailyActivity(days)).map((point) => {
    sent += point.sent;
    return { day: point.day, sent };
  });
}

/** Verification scores in 10-point bins. Shows how much of the list sits near
 *  the send threshold rather than comfortably above it. */
export async function verificationScoreDistribution(): Promise<{ bucket: string; count: number }[]> {
  const rows = await all<{ bin: number; count: number }>(
    `SELECT (score / 10) * 10 AS bin, COUNT(*) count
     FROM latest_verification GROUP BY bin ORDER BY bin`,
  );
  return rows.map((row) => ({
    bucket: row.bin >= 100 ? "100" : `${row.bin}-${row.bin + 9}`,
    count: row.count,
  }));
}

export interface CoverageRow {
  segment: string;
  total: number;
  contacted: number;
  remaining: number;
}

/** How much of each industry has actually been worked, versus how much is left. */
export async function coverageByIndustry(): Promise<CoverageRow[]> {
  return await all<CoverageRow>(
    `SELECT
       COALESCE(c.industry, '(none)') AS segment,
       COUNT(DISTINCT c.id) AS total,
       COUNT(DISTINCT CASE WHEN m.id IS NOT NULL THEN c.id END) AS contacted,
       COUNT(DISTINCT c.id) - COUNT(DISTINCT CASE WHEN m.id IS NOT NULL THEN c.id END) AS remaining
     FROM contacts c
     LEFT JOIN messages m
       ON m.contact_id = c.id AND m.direction = 'outbound' AND m.status IN ${SENT_STATUSES}
     GROUP BY COALESCE(c.industry, '(none)')
     ORDER BY total DESC`,
  );
}

/** Step-to-step survival: how many make it to each touch, and why they drop out. */
export async function stepFunnel(): Promise<{ step: number; reached: number; repliedAfter: number }[]> {
  return await all(
    `SELECT
       m.step_number AS step,
       COUNT(DISTINCT m.enrollment_id) AS reached,
       COUNT(DISTINCT CASE WHEN r.classification IN ('positive','negative','neutral')
                           THEN r.enrollment_id END) AS repliedAfter
     FROM messages m
     LEFT JOIN replies r
       ON r.enrollment_id = m.enrollment_id
      AND r.received_at > COALESCE(m.sent_at, m.created_at)
     WHERE m.direction = 'outbound' AND m.status IN ${SENT_STATUSES} AND m.step_number IS NOT NULL
     GROUP BY m.step_number ORDER BY m.step_number`,
  );
}

// ── Segments ──────────────────────────────────────────────────────────────

export interface SegmentRow {
  segment: string;
  contacts: number;
  contacted: number;
  replies: number;
  positive: number;
  negative: number;
  replyRate: number;
  positiveRate: number;
}

/**
 * Performance sliced by any contact column. The whole point of a 239-row list
 * split across industries and tiers is learning *which slice* answers — a
 * single blended reply rate hides that completely.
 */
export async function bySegment(column: "industry" | "tier" | "role_group" | "source_sheet"): Promise<SegmentRow[]> {
  const rows = await all<{
    segment: string | null;
    contacts: number;
    contacted: number;
    replies: number;
    positive: number;
    negative: number;
  }>(
    `SELECT
       c.${column} AS segment,
       COUNT(DISTINCT c.id) AS contacts,
       COUNT(DISTINCT CASE WHEN m.id IS NOT NULL THEN c.id END) AS contacted,
       COUNT(DISTINCT CASE WHEN r.classification IN ('positive','negative','neutral') THEN r.id END) AS replies,
       COUNT(DISTINCT CASE WHEN r.classification = 'positive' THEN r.id END) AS positive,
       COUNT(DISTINCT CASE WHEN r.classification = 'negative' THEN r.id END) AS negative
     FROM contacts c
     LEFT JOIN messages m
       ON m.contact_id = c.id AND m.direction = 'outbound' AND m.status IN ${SENT_STATUSES}
     LEFT JOIN replies r ON r.contact_id = c.id
     GROUP BY c.${column}
     ORDER BY contacts DESC`,
  );

  return rows.map((row) => ({
    segment: row.segment ?? "(none)",
    contacts: row.contacts,
    contacted: row.contacted,
    replies: row.replies,
    positive: row.positive,
    negative: row.negative,
    replyRate: row.contacted > 0 ? (row.replies / row.contacted) * 100 : 0,
    positiveRate: row.replies > 0 ? (row.positive / row.replies) * 100 : 0,
  }));
}

// ── Sequence step performance ─────────────────────────────────────────────

export interface StepRow {
  step: number;
  templateName: string | null;
  sent: number;
  replies: number;
  positive: number;
  replyRate: number;
}

/**
 * Which touch actually earns the replies. Follow-ups routinely outperform the
 * opener, and without this you would never know which one to cut.
 */
export async function byStep(): Promise<StepRow[]> {
  return await all<StepRow>(
    `SELECT
       m.step_number AS step,
       t.name AS templateName,
       COUNT(DISTINCT m.id) AS sent,
       COUNT(DISTINCT CASE WHEN r.classification IN ('positive','negative','neutral') THEN r.id END) AS replies,
       COUNT(DISTINCT CASE WHEN r.classification = 'positive' THEN r.id END) AS positive,
       CASE WHEN COUNT(DISTINCT m.id) > 0
            THEN (COUNT(DISTINCT CASE WHEN r.classification IN ('positive','negative','neutral') THEN r.id END) * 100.0)
                 / COUNT(DISTINCT m.id)
            ELSE 0 END AS replyRate
     FROM messages m
     LEFT JOIN templates t ON t.id = m.template_id
     -- Attribute a reply to the step that immediately preceded it.
     LEFT JOIN replies r
       ON r.enrollment_id = m.enrollment_id
      AND r.received_at > COALESCE(m.sent_at, m.created_at)
     WHERE m.direction = 'outbound' AND m.status IN ${SENT_STATUSES} AND m.step_number IS NOT NULL
     GROUP BY m.step_number, t.name
     ORDER BY m.step_number`,
  );
}

// ── Verification & deliverability ─────────────────────────────────────────

export interface VerificationBreakdown {
  status: string;
  count: number;
  avgScore: number;
}

export async function verificationBreakdown(): Promise<VerificationBreakdown[]> {
  return await all<VerificationBreakdown>(
    `SELECT status, COUNT(*) count, ROUND(AVG(score), 1) avgScore
     FROM latest_verification GROUP BY status ORDER BY count DESC`,
  );
}

export async function verificationFlags() {
  return await get<{ role: number; catchAll: number; free: number; unverified: number }>(
    `SELECT
       (SELECT COUNT(*) FROM latest_verification WHERE is_role = 1) AS role,
       (SELECT COUNT(*) FROM latest_verification WHERE is_catch_all = 1) AS catchAll,
       (SELECT COUNT(*) FROM latest_verification WHERE is_free_provider = 1) AS free,
       (SELECT COUNT(*) FROM contacts c
         WHERE c.email_normalized IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM latest_verification v WHERE v.contact_id = c.id)) AS unverified`,
  );
}

// ── Reply mix ─────────────────────────────────────────────────────────────

export async function replyMix(): Promise<{ classification: string; count: number; avgConfidence: number }[]> {
  return await all(
    `SELECT classification, COUNT(*) count, ROUND(AVG(confidence), 2) avgConfidence
     FROM replies GROUP BY classification ORDER BY count DESC`,
  );
}

/**
 * How long people take to answer. Drives the follow-up delay: if the median
 * reply lands on day 2, a 7-day gap is leaving the thread cold for no reason.
 */
export async function timeToReply(): Promise<{ bucket: string; count: number }[]> {
  return await all(
    `SELECT
       CASE
         WHEN hours < 4 THEN 'under 4h'
         WHEN hours < 24 THEN '4-24h'
         WHEN hours < 72 THEN '1-3 days'
         WHEN hours < 168 THEN '3-7 days'
         ELSE 'over a week'
       END AS bucket,
       COUNT(*) AS count
     FROM (
       SELECT (julianday(r.received_at) - julianday(COALESCE(m.sent_at, m.created_at))) * 24 AS hours
       FROM replies r
       JOIN messages m ON m.enrollment_id = r.enrollment_id AND m.direction = 'outbound'
       WHERE r.classification IN ('positive','negative','neutral')
       GROUP BY r.id
     )
     GROUP BY bucket`,
  );
}

// ── Enrollment state ──────────────────────────────────────────────────────

export async function enrollmentStates(): Promise<{ status: string; count: number }[]> {
  return await all(`SELECT status, COUNT(*) count FROM enrollments GROUP BY status ORDER BY count DESC`);
}

export async function campaignSummaries() {
  return await all<{
    id: number;
    name: string;
    status: string;
    /** Still going: pending or active. Not a running total of everyone ever added. */
    enrolled: number;
    everEnrolled: number;
    sent: number;
    replies: number;
    positive: number;
    daily_cap: number;
  }>(
    `SELECT
       ca.id, ca.name, ca.status, ca.daily_cap,
       COUNT(DISTINCT CASE WHEN e.status IN ('pending','active') THEN e.id END) AS enrolled,
       COUNT(DISTINCT e.id) AS everEnrolled,
       COUNT(DISTINCT CASE WHEN m.status = 'sent' THEN m.id END) AS sent,
       COUNT(DISTINCT CASE WHEN r.classification IN ('positive','negative','neutral') THEN r.id END) AS replies,
       COUNT(DISTINCT CASE WHEN r.classification = 'positive' THEN r.id END) AS positive
     FROM campaigns ca
     LEFT JOIN enrollments e ON e.campaign_id = ca.id
     LEFT JOIN messages m ON m.enrollment_id = e.id AND m.direction = 'outbound'
     LEFT JOIN replies r ON r.enrollment_id = e.id
     GROUP BY ca.id
     ORDER BY ca.created_at DESC`,
  );
}

// ── Contacts table ────────────────────────────────────────────────────────

export interface ContactFilter {
  search?: string;
  sheet?: string;
  industry?: string;
  roleGroup?: string;
  priority?: string;
  verification?: string;
  enrollment?: string;
  limit?: number;
  offset?: number;
}

export async function contactRows(filter: ContactFilter = {}) {
  // Contacts without a verified email are not people you can act on, so they
  // live on the Inventory view (as companies awaiting enrichment), not here.
  const where: string[] = ["c.email_normalized IS NOT NULL"];
  const params: unknown[] = [];

  if (filter.search) {
    where.push(`(c.company LIKE ? OR c.full_name LIKE ? OR c.email_normalized LIKE ? OR c.role LIKE ?)`);
    const like = `%${filter.search}%`;
    params.push(like, like, like, like);
  }
  if (filter.sheet) { where.push(`c.source_sheet = ?`); params.push(filter.sheet); }
  if (filter.industry) { where.push(`c.industry = ?`); params.push(filter.industry); }
  if (filter.roleGroup) {
    where.push(`c.role_group = ?`);
    params.push(filter.roleGroup);
  }
  if (filter.verification) {
    if (filter.verification === "unverified") where.push(`v.status IS NULL`);
    else { where.push(`v.status = ?`); params.push(filter.verification); }
  }
  if (filter.enrollment) {
    if (filter.enrollment === "none") where.push(`e.id IS NULL`);
    else { where.push(`e.status = ?`); params.push(filter.enrollment); }
  }

  const limit = filter.limit ?? 100;
  const offset = filter.offset ?? 0;

  const rows = all<Record<string, unknown>>(
    `SELECT
       c.id, c.company, c.full_name, c.first_name, c.name_quality, c.role, c.role_group,
       c.industry, c.tier, c.email_normalized AS email, c.domain,
       c.source_sheet, c.linkedin, c.outreach_channel, c.is_yc,
       v.status AS verification_status, v.score AS verification_score,
       v.is_role, v.is_catch_all, v.reasons AS verification_reasons,
       e.id AS enrollment_id, e.status AS enrollment_status, e.current_step, e.next_send_at,
       ca.name AS campaign_name,
       (SELECT COUNT(*) FROM messages m
         WHERE m.contact_id = c.id AND m.direction = 'outbound' AND m.status IN ${SENT_STATUSES}) AS messages_sent,
       (SELECT r.classification FROM replies r WHERE r.contact_id = c.id
         ORDER BY r.received_at DESC LIMIT 1) AS last_reply,
       (SELECT COUNT(*) FROM suppressions s
         WHERE s.value = c.email_normalized OR s.value = c.domain) AS suppressed
     FROM contacts c
     LEFT JOIN latest_verification v ON v.contact_id = c.id
     LEFT JOIN enrollments e ON e.contact_id = c.id
     LEFT JOIN campaigns ca ON ca.id = e.campaign_id
     WHERE ${where.join(" AND ")}
     ORDER BY c.company COLLATE NOCASE, c.full_name COLLATE NOCASE
     LIMIT ? OFFSET ?`,
    [...params, limit, offset],
  );

  const total =
    (await get<{ n: number }>(
      `SELECT COUNT(*) n FROM contacts c
       LEFT JOIN latest_verification v ON v.contact_id = c.id
       LEFT JOIN enrollments e ON e.contact_id = c.id
       WHERE ${where.join(" AND ")}`,
      params,
    ))?.n ?? 0;

  return { rows, total };
}

export async function filterOptions() {
  return {
    sheets: (await all<{ value: string }>(`SELECT DISTINCT source_sheet value FROM contacts ORDER BY value`)).map((r) => r.value),
    industries: (await all<{ value: string }>(
      `SELECT DISTINCT industry value FROM contacts WHERE industry IS NOT NULL ORDER BY value`,
    )).map((r) => r.value),
    tiers: (await all<{ value: string }>(
      `SELECT DISTINCT tier value FROM contacts WHERE tier IS NOT NULL ORDER BY value`,
    )).map((r) => r.value),
    roleGroups: await all<{ value: string; n: number }>(
      `SELECT role_group value, COUNT(*) n FROM contacts
       WHERE role_group IS NOT NULL GROUP BY role_group ORDER BY n DESC`,
    ),
  };
}

// ── Partner view ──────────────────────────────────────────────────────────
// The company is the unit here, not the person. A company with four contacts
// where one replied has been *answered*, and a per-person table makes that
// hard to see at a glance.

export interface PartnerCompany {
  company: string;
  industry: string | null;
  tier: string | null;
  stage: string | null;
  domain: string | null;
  /** Best priority across everyone at the company: HOT beats WARM beats none. */
  priority: string | null;
  people: number;
  deliverable: number;
  enrolled: number;
  /** People who have been emailed. */
  liveSent: number;
  replies: number;
  positive: number;
  negative: number;
  suppressed: number;
  lastTouch: string | null;
  names: string;
  /** Contacts with an address on file. Every stored address is Apollo-verified
   *  since the guessed-email purge, so this is the count of people you could
   *  actually email today. */
  verifiedEmails: number;
  /** YC company: warm channel, structurally excluded from cold enrollment. */
  isYc: number;
}

export interface PartnerIndustry {
  industry: string;
  companies: PartnerCompany[];
  totals: {
    companies: number;
    people: number;
    deliverable: number;
    enrolled: number;
    liveSent: number;
    replies: number;
    positive: number;
  };
}

/**
 * Every company, grouped by industry, with its outreach state.
 *
 * Grouped in JS rather than by a second query so the industry totals are
 * guaranteed to be the sum of the rows displayed under them — a separate
 * aggregate query can drift from the detail it claims to summarize.
 */
export async function partnersByIndustry(): Promise<PartnerIndustry[]> {
  const rows = await all<PartnerCompany>(
    `SELECT
       c.company,
       MAX(c.industry) AS industry,
       MAX(c.tier) AS tier,
       MAX(c.stage) AS stage,
       MAX(c.domain) AS domain,
       CASE
         WHEN SUM(CASE WHEN c.priority = 'HOT' THEN 1 ELSE 0 END) > 0 THEN 'HOT'
         WHEN SUM(CASE WHEN c.priority = 'WARM' THEN 1 ELSE 0 END) > 0 THEN 'WARM'
         ELSE NULL
       END AS priority,
       COUNT(DISTINCT c.id) AS people,
       COUNT(DISTINCT CASE WHEN v.status = 'valid' THEN c.id END) AS deliverable,
       COUNT(DISTINCT CASE WHEN e.status IN ('pending','active') THEN e.id END) AS enrolled,
       COUNT(DISTINCT CASE WHEN m.status = 'sent' THEN c.id END) AS liveSent,
       COUNT(DISTINCT CASE WHEN r.classification IN ('positive','negative','neutral') THEN r.id END) AS replies,
       COUNT(DISTINCT CASE WHEN r.classification = 'positive' THEN r.id END) AS positive,
       COUNT(DISTINCT CASE WHEN r.classification = 'negative' THEN r.id END) AS negative,
       (SELECT COUNT(*) FROM suppressions s
         WHERE s.value = MAX(c.domain)
            OR s.value IN (SELECT c2.email_normalized FROM contacts c2 WHERE c2.company = c.company)
       ) AS suppressed,
       MAX(COALESCE(m.sent_at, m.created_at)) AS lastTouch,
       GROUP_CONCAT(DISTINCT c.full_name) AS names,
       COUNT(DISTINCT CASE WHEN c.email_normalized IS NOT NULL THEN c.id END) AS verifiedEmails,
       MAX(c.is_yc) AS isYc
     FROM contacts c
     LEFT JOIN latest_verification v ON v.contact_id = c.id
     LEFT JOIN enrollments e ON e.contact_id = c.id
     LEFT JOIN messages m ON m.contact_id = c.id AND m.direction = 'outbound'
     LEFT JOIN replies r ON r.contact_id = c.id
     GROUP BY c.company
     HAVING COUNT(DISTINCT CASE WHEN c.email_normalized IS NOT NULL THEN c.id END) > 0
     ORDER BY c.company`,
  );

  const grouped = new Map<string, PartnerCompany[]>();
  for (const row of rows) {
    const key = row.industry ?? "Other";
    const bucket = grouped.get(key);
    if (bucket) bucket.push(row);
    else grouped.set(key, [row]);
  }

  return [...grouped.entries()]
    .map(([industry, companies]) => ({
      industry,
      companies: companies.sort((a, b) => {
        const rank = (p: string | null) => (p === "HOT" ? 0 : p === "WARM" ? 1 : 2);
        return rank(a.priority) - rank(b.priority) || a.company.localeCompare(b.company);
      }),
      totals: {
        companies: companies.length,
        people: companies.reduce((n, c) => n + c.people, 0),
        deliverable: companies.reduce((n, c) => n + c.deliverable, 0),
        enrolled: companies.reduce((n, c) => n + c.enrolled, 0),
        liveSent: companies.reduce((n, c) => n + c.liveSent, 0),
        replies: companies.reduce((n, c) => n + c.replies, 0),
        positive: companies.reduce((n, c) => n + c.positive, 0),
      },
    }))
    .sort((a, b) => b.totals.companies - a.totals.companies);
}

/** The people at one company, for the expandable detail row. */
export function peopleAtCompany(company: string) {
  return all<Record<string, unknown>>(
    `SELECT
       c.id, c.full_name, c.role, c.email_normalized AS email, c.priority,
       c.name_quality, c.linkedin, c.outreach_channel,
       v.status AS verification_status, v.score AS verification_score,
       e.status AS enrollment_status, e.current_step,
       (SELECT COUNT(*) FROM messages m
         WHERE m.contact_id = c.id AND m.direction = 'outbound' AND m.status = 'sent') AS live_sent,
       (SELECT r.classification FROM replies r WHERE r.contact_id = c.id
         ORDER BY r.received_at DESC LIMIT 1) AS last_reply
     FROM contacts c
     LEFT JOIN latest_verification v ON v.contact_id = c.id
     LEFT JOIN enrollments e ON e.contact_id = c.id
     WHERE c.company = ?
     ORDER BY CASE c.priority WHEN 'HOT' THEN 0 WHEN 'WARM' THEN 1 ELSE 2 END, c.full_name`,
    [company],
  );
}

export function contactDetail(contactId: number) {
  return {
    contact: get<Record<string, unknown>>(`SELECT * FROM contacts WHERE id = ?`, [contactId]),
    verification: get<Record<string, unknown>>(`SELECT * FROM latest_verification WHERE contact_id = ?`, [contactId]),
    enrollments: all<Record<string, unknown>>(
      `SELECT e.*, ca.name AS campaign_name FROM enrollments e
       JOIN campaigns ca ON ca.id = e.campaign_id WHERE e.contact_id = ? ORDER BY e.enrolled_at DESC`,
      [contactId],
    ),
    messages: all<Record<string, unknown>>(
      `SELECT * FROM messages WHERE contact_id = ? ORDER BY COALESCE(sent_at, received_at, created_at) ASC`,
      [contactId],
    ),
    replies: all<Record<string, unknown>>(
      `SELECT * FROM replies WHERE contact_id = ? ORDER BY received_at DESC`,
      [contactId],
    ),
  };
}

/**
 * Every person, keyed by company. The partner grid renders 222 boxes and a
 * detail dialog for whichever one is open; fetching people per click would
 * mean a server round trip on every box. At 239 rows the whole set is smaller
 * than one page of the contacts table, so it ships with the page instead.
 */
export async function allPeopleByCompany(): Promise<Record<string, PartnerPerson[]>> {
  const rows = await all<PartnerPerson & { company: string }>(
    `SELECT
       c.id, c.company, c.full_name, c.role, c.email_normalized AS email,
       c.priority, c.name_quality, c.linkedin, c.outreach_channel, c.source_sheet,
       v.status AS verification_status, v.score AS verification_score, v.reasons AS verification_reasons,
       e.status AS enrollment_status, e.current_step, e.next_send_at,
       (SELECT COUNT(*) FROM messages m
         WHERE m.contact_id = c.id AND m.direction = 'outbound' AND m.status = 'sent') AS live_sent,
       (SELECT r.classification FROM replies r WHERE r.contact_id = c.id
         ORDER BY r.received_at DESC LIMIT 1) AS last_reply
     FROM contacts c
     LEFT JOIN latest_verification v ON v.contact_id = c.id
     LEFT JOIN enrollments e ON e.contact_id = c.id
     ORDER BY c.full_name`,
  );

  const out: Record<string, PartnerPerson[]> = {};
  for (const row of rows) {
    (out[row.company] ??= []).push(row);
  }
  return out;
}

export interface PartnerPerson {
  id: number;
  full_name: string | null;
  role: string | null;
  email: string | null;
  priority: string | null;
  name_quality: string;
  linkedin: string | null;
  outreach_channel: string | null;
  source_sheet: string;
  verification_status: string | null;
  verification_score: number | null;
  verification_reasons: string | null;
  enrollment_status: string | null;
  current_step: number | null;
  next_send_at: string | null;
  live_sent: number;
  last_reply: string | null;
}
