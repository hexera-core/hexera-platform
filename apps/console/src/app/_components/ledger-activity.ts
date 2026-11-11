/** Aggregation for the billing activity charts: signed ledger entries into UTC days.
 *
 * The ledger is a stream, not a series - the page folds it into per-day buckets here so the
 * charts render nothing but numbers. Days are UTC: entry.created_at is an ISO instant, and the
 * alternative (the reader's zone) is unknowable on the server; the ledger table's When column
 * keeps local time for the entries themselves. */

export type LedgerAmount = { amount: number; created_at: string | null };

export type DayActivity = {
  /** YYYY-MM-DD (UTC) - the bucket's identity. */
  day: string;
  /** "Nov 4" - drawn under the bar. */
  label: string;
  /** Credits spent that day, as a positive magnitude. */
  spent: number;
  /** Credits granted or refunded that day. */
  granted: number;
};

const DAY_MS = 86_400_000;

const DAY_LABEL = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

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

/** The last `days` UTC days ending today, each with the credits spent and granted in it.
 *  Entries without a parseable created_at, or older than the window, do not move a bucket. */
export function activityWindow(
  entries: LedgerAmount[],
  now = new Date(),
  days = 30,
): DayActivity[] {
  const end = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const start = end - (days - 1) * DAY_MS;
  const buckets: DayActivity[] = [];
  for (let i = 0; i < days; i += 1) {
    const t = start + i * DAY_MS;
    buckets.push({ day: dayKey(t), label: DAY_LABEL.format(t), spent: 0, granted: 0 });
  }
  for (const entry of entries) {
    if (!entry.created_at) {
      continue;
    }
    const dayStart = utcDayStart(entry.created_at);
    if (dayStart === null) {
      continue;
    }
    const index = Math.round((dayStart - start) / DAY_MS);
    const bucket = index >= 0 && index < days ? buckets[index] : null;
    if (!bucket) {
      continue;
    }
    if (entry.amount < 0) {
      bucket.spent += -entry.amount;
    } else {
      bucket.granted += entry.amount;
    }
  }
  return buckets;
}

export function activityTotals(days: DayActivity[]): { spent: number; granted: number } {
  let spent = 0;
  let granted = 0;
  for (const day of days) {
    spent += day.spent;
    granted += day.granted;
  }
  return { spent, granted };
}

/** End-of-day balance per day, walked back from the balance the API reports now.
 *
 * `full` says the fetched entries reach back to the beginning of the ledger (no cursor was
 * left). Only then are days before the earliest entry drawn: with a truncated window they
 * would claim a balance that un-read entries could change, so they come back null and the
 * chart leaves them empty. */
export function balanceSeries(
  days: DayActivity[],
  endBalance: number,
  full: boolean,
): (number | null)[] {
  const count = days.length;
  if (!count) {
    return [];
  }
  // eod[i] is the balance at the end of day i: tomorrow's end-of-day minus tomorrow's net.
  const eod = new Array<number>(count);
  eod[count - 1] = endBalance;
  for (let i = count - 2; i >= 0; i -= 1) {
    eod[i] = eod[i + 1] - (days[i + 1].granted - days[i + 1].spent);
  }
  const out = new Array<number | null>(count).fill(null);
  const earliestActivity = days.findIndex((d) => d.spent !== 0 || d.granted !== 0);
  const from = full ? 0 : earliestActivity;
  if (from < 0) {
    return out;
  }
  for (let i = from; i < count; i += 1) {
    out[i] = eod[i];
  }
  return out;
}
