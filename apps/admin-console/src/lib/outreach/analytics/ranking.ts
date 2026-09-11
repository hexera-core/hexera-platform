/**
 * Computed company ranking, replacing the hand-assigned HOT/WARM column.
 *
 * HOT/WARM was a guess made before a single email went out. Once real replies
 * exist, the list can rank itself from what actually happened: which
 * industries answer, which tiers answer, which roles answer. This module
 * turns that into one score per company.
 *
 * Two things keep it honest:
 *
 *  1. **Shrinkage.** A segment with one reply out of two contacts is not a
 *     90% reply rate, it is noise. Every segment rate is pulled toward the
 *     global rate in proportion to how little data supports it, so a segment
 *     only moves the ranking once it has earned the right to.
 *
 *  2. **A readiness gate.** Below a floor of real sends and real replies
 *     there is nothing to learn, so `ranking()` reports `ready: false` and
 *     the UI stays alphabetical rather than showing a confident-looking
 *     ordering built on four data points.
 */
import { all, scalar } from "../db";

/** Pseudo-count for shrinkage. Roughly "how many observations before a
 *  segment's own rate outweighs the global average". */
const PRIOR_STRENGTH = 8;

/** Nothing is ranked until the list has produced at least this much signal. */
const MIN_CONTACTED = 30;
const MIN_REPLIES = 5;

export interface Readiness {
  ready: boolean;
  contacted: number;
  replies: number;
  minContacted: number;
  minReplies: number;
  /** Plain sentence for the UI to show while it waits. */
  message: string;
}

export async function readiness(): Promise<Readiness> {
  const contacted =
    (await scalar<number>(
      `SELECT COUNT(DISTINCT contact_id) FROM messages
       WHERE direction = 'outbound' AND status = 'sent'`,
    )) ?? 0;
  const replies =
    (await scalar<number>(
      `SELECT COUNT(*) FROM replies WHERE classification IN ('positive','negative','neutral')`,
    )) ?? 0;

  const ready = contacted >= MIN_CONTACTED && replies >= MIN_REPLIES;

  return {
    ready,
    contacted,
    replies,
    minContacted: MIN_CONTACTED,
    minReplies: MIN_REPLIES,
    message: ready
      ? `Ranked from ${replies} replies across ${contacted} contacted people.`
      : `Ranking turns on after ${MIN_CONTACTED} people have been contacted and ${MIN_REPLIES} have replied. So far: ${contacted} contacted, ${replies} replies.`,
  };
}

interface SegmentRow {
  segment: string | null;
  contacted: number;
  positive: number;
}

/**
 * Positive-reply rate per value of one contact column, shrunk toward the
 * global rate. Returns a lookup keyed by the column value.
 */
async function shrunkRates(column: "industry" | "tier" | "role", globalRate: number): Promise<Map<string, number>> {
  const rows = await all<SegmentRow>(
    `SELECT
       c.${column} AS segment,
       COUNT(DISTINCT CASE WHEN m.id IS NOT NULL THEN c.id END) AS contacted,
       COUNT(DISTINCT CASE WHEN r.classification = 'positive' THEN r.id END) AS positive
     FROM contacts c
     LEFT JOIN messages m
       ON m.contact_id = c.id AND m.direction = 'outbound' AND m.status = 'sent'
     LEFT JOIN replies r ON r.contact_id = c.id
     GROUP BY c.${column}`,
  );

  const out = new Map<string, number>();
  for (const row of rows) {
    if (!row.segment || row.contacted === 0) continue;
    const rate =
      (row.positive + PRIOR_STRENGTH * globalRate) / (row.contacted + PRIOR_STRENGTH);
    out.set(row.segment, rate);
  }
  return out;
}

export interface CompanyScore {
  company: string;
  /** 0-100. Higher means the segments this company sits in answer more often. */
  score: number;
  /** Which segments drove it, for the tooltip. */
  basis: string[];
}

/**
 * One score per company, blended from the segments it belongs to.
 *
 * A company inherits the behaviour of its industry, its tier, and the roles
 * of the people on file. None of those is decisive alone, so the score is
 * their mean, and a company whose segments have no data at all simply lands
 * on the global rate rather than being pushed to the bottom.
 */
export async function companyScores(): Promise<Map<string, CompanyScore>> {
  const state = await readiness();
  const scores = new Map<string, CompanyScore>();
  if (!state.ready) return scores;

  const totalPositive =
    (await scalar<number>(`SELECT COUNT(*) FROM replies WHERE classification = 'positive'`)) ?? 0;
  const globalRate = state.contacted > 0 ? totalPositive / state.contacted : 0;

  const byIndustry = await shrunkRates("industry", globalRate);
  const byTier = await shrunkRates("tier", globalRate);
  const byRole = await shrunkRates("role", globalRate);

  const companies = await all<{ company: string; industry: string | null; tier: string | null; roles: string | null }>(
    `SELECT company,
            MAX(industry) AS industry,
            MAX(tier) AS tier,
            GROUP_CONCAT(role, '|') AS roles
     FROM contacts GROUP BY company`,
  );

  // Normalize against the best raw blend so the top of the list reads 100
  // rather than some unintuitive fraction.
  const raw = new Map<string, { value: number; basis: string[] }>();
  let best = 0;

  for (const row of companies) {
    const parts: number[] = [];
    const basis: string[] = [];

    const industryRate = row.industry ? byIndustry.get(row.industry) : undefined;
    if (industryRate !== undefined) {
      parts.push(industryRate);
      basis.push(`${row.industry} replies at ${(industryRate * 100).toFixed(1)}%`);
    }

    const tierRate = row.tier ? byTier.get(row.tier) : undefined;
    if (tierRate !== undefined) {
      parts.push(tierRate);
      basis.push(`${row.tier} replies at ${(tierRate * 100).toFixed(1)}%`);
    }

    const roleRates = (row.roles ?? "")
      .split("|")
      .map((role) => byRole.get(role))
      .filter((r): r is number => r !== undefined);
    if (roleRates.length) {
      const mean = roleRates.reduce((a, b) => a + b, 0) / roleRates.length;
      parts.push(mean);
      basis.push(`roles on file reply at ${(mean * 100).toFixed(1)}%`);
    }

    const value = parts.length
      ? parts.reduce((a, b) => a + b, 0) / parts.length
      : globalRate;

    if (value > best) best = value;
    raw.set(row.company, { value, basis });
  }

  for (const [company, { value, basis }] of raw) {
    scores.set(company, {
      company,
      score: best > 0 ? Math.round((value / best) * 100) : 0,
      basis: basis.length ? basis : ["no segment data yet, using the list average"],
    });
  }

  return scores;
}
