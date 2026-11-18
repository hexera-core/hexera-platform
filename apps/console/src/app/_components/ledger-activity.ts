/** Aggregation for the billing spend chart: ledger entries into UTC days by source.
 *
 * The ledger is a stream, not a series - the page folds it into per-day buckets here so the
 * chart renders nothing but numbers. Only spend counts: grants and purchases are positive
 * entries and are answered by the balance card and the ledger table, not by a "why did my
 * credits go down" picture. Days are UTC: entry.created_at is an ISO instant, and the
 * alternative (the reader's zone) is unknowable on the server; the ledger table's When column
 * keeps local time for the entries themselves. */

export type LedgerAmount = { amount: number; created_at: string | null };

export type SpendSource = {
  /** The identity a day's parts stack by - a run id, or "other" for spend not tied to a run. */
  key: string;
  /** What the legend and the tooltip call it. */
  label: string;
  /** Credits this source spent across the whole window. */
  amount: number;
};

export type DaySpend = {
  /** YYYY-MM-DD (UTC) - the bucket's identity. */
  day: string;
  /** "Nov 4" - drawn under the bar. */
  label: string;
  /** Credits spent that day across every source. */
  total: number;
  /** Per-source spend, in the same ranked order as `sources`. */
  parts: { key: string; label: string; amount: number }[];
};

export type SpendSeries = { days: DaySpend[]; sources: SpendSource[] };

const DAY_MS = 86_400_000;

const DAY_LABEL = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

const OTHER_SOURCE = { key: "other", label: "Other" };

/** How many sources get their own colour; the long tail merges into "Other" so the legend
 *  stays a legend and not a second table. */
export const CHART_SOURCES = 4;

function utcDayStart(iso: string): number | null {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) {
    return null;
  }
  const d = new Date(t);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

function dayKey(t: number): string {
  return new Date(t).toISOString().slice(0, 10);
}

/** The last `days` UTC days ending today, spend split per source.
 *
 * `sourceOf` names each entry's source (the page resolves a ledger reason's run id to a run
 * title, falling back to its short id; anything without a run is "Other"). Entries that are
 * not spend, carry no parseable created_at, or sit outside the window move nothing. */
export function spendByDay<T extends LedgerAmount>(
  entries: T[],
  sourceOf: (entry: T) => { key: string; label: string },
  now = new Date(),
  days = 30,
): SpendSeries {
  const end = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const start = end - (days - 1) * DAY_MS;

  const perDay = Array.from({ length: days }, () => new Map<string, { label: string; amount: number }>());
  const totals = new Map<string, SpendSource>();
  for (const entry of entries) {
    if (entry.amount >= 0 || !entry.created_at) {
      continue;
    }
    const dayStart = utcDayStart(entry.created_at);
    if (dayStart === null) {
      continue;
    }
    const index = Math.round((dayStart - start) / DAY_MS);
    if (index < 0 || index >= days) {
      continue;
    }
    const src = sourceOf(entry);
    const magnitude = -entry.amount;
    const bucket = perDay[index].get(src.key) ?? { label: src.label, amount: 0 };
    bucket.amount += magnitude;
    perDay[index].set(src.key, bucket);
    const total = totals.get(src.key) ?? { key: src.key, label: src.label, amount: 0 };
    total.amount += magnitude;
    totals.set(src.key, total);
  }

  // Rank sources by what they spent; beyond the palette the rest fold into "Other".
  const ranked = [...totals.values()].sort((a, b) => b.amount - a.amount);
  const top = ranked.slice(0, CHART_SOURCES);
  const rest = ranked.slice(CHART_SOURCES);
  const keep = new Set(top.map((s) => s.key));
  const sources: SpendSource[] = [...top];
  if (rest.length) {
    sources.push({ ...OTHER_SOURCE, amount: rest.reduce((sum, s) => sum + s.amount, 0) });
  }

  const out: DaySpend[] = [];
  for (let i = 0; i < days; i += 1) {
    const t = start + i * DAY_MS;
    const parts = [];
    let total = 0;
    let other = 0;
    for (const [key, part] of perDay[i]) {
      total += part.amount;
      if (!keep.has(key)) {
        other += part.amount;
      }
    }
    for (const src of sources) {
      if (src.key === OTHER_SOURCE.key) {
        if (other > 0) {
          parts.push({ key: OTHER_SOURCE.key, label: OTHER_SOURCE.label, amount: other });
        }
        continue;
      }
      const part = perDay[i].get(src.key);
      if (part) {
        parts.push({ key: src.key, label: part.label, amount: part.amount });
      }
    }
    out.push({ day: dayKey(t), label: DAY_LABEL.format(t), total, parts });
  }
  return { days: out, sources };
}

export function spendTotal(days: DaySpend[]): number {
  return days.reduce((sum, day) => sum + day.total, 0);
}
