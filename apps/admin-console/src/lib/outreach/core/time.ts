/**
 * Timezone-aware scheduling helpers, built on Intl rather than a date library.
 *
 * Everything persisted is a UTC ISO string; everything a human reasons about
 * ("send between 8am and 5pm on weekdays") is in the campaign's timezone. This
 * module is the only place those two views are allowed to meet.
 */

export function nowIso(): string {
  return new Date().toISOString();
}

export function toIso(date: Date): string {
  return date.toISOString();
}

export interface ZonedParts {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number;
  minute: number;
  second: number;
  /** ISO weekday: Monday = 1 … Sunday = 7 */
  weekday: number;
}

const WEEKDAY_TO_ISO: Record<string, number> = {
  Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7,
};

const formatterCache = new Map<string, Intl.DateTimeFormat>();

function formatter(timeZone: string): Intl.DateTimeFormat {
  let cached = formatterCache.get(timeZone);
  if (!cached) {
    cached = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      weekday: "short",
    });
    formatterCache.set(timeZone, cached);
  }
  return cached;
}

export function zonedParts(date: Date, timeZone: string): ZonedParts {
  const parts = formatter(timeZone).formatToParts(date);
  const pick = (type: string) => parts.find((p) => p.type === type)?.value ?? "0";
  // Intl renders midnight as hour "24" in some locales/engines; normalize it.
  const hour = Number(pick("hour")) % 24;
  return {
    year: Number(pick("year")),
    month: Number(pick("month")),
    day: Number(pick("day")),
    hour,
    minute: Number(pick("minute")),
    second: Number(pick("second")),
    weekday: WEEKDAY_TO_ISO[pick("weekday")] ?? 1,
  };
}

/** 'YYYY-MM-DD' as seen in `timeZone`. The key used by the daily send ledger. */
export function dayKey(date: Date, timeZone: string): string {
  const p = zonedParts(date, timeZone);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

/**
 * The inverse of zonedParts: given a wall-clock time in `timeZone`, find the
 * UTC instant for it.
 *
 * Intl only converts one way, so this measures the zone's offset at a guessed
 * instant and corrects. Two passes because the first correction can itself
 * cross a DST boundary and land at a different offset.
 */
export function zonedTimeToUtc(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute: number,
  timeZone: string,
): Date {
  let guess = Date.UTC(year, month - 1, day, hour, minute, 0, 0);
  for (let pass = 0; pass < 2; pass++) {
    const seen = zonedParts(new Date(guess), timeZone);
    const seenAsUtc = Date.UTC(seen.year, seen.month - 1, seen.day, seen.hour, seen.minute, 0, 0);
    const drift = seenAsUtc - guess;
    if (drift === 0) break;
    guess -= drift;
  }
  return new Date(guess);
}

export function parseSendDays(spec: string): Set<number> {
  const days = spec
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isInteger(n) && n >= 1 && n <= 7);
  // An empty or malformed spec would silently disable the campaign forever;
  // fall back to weekdays instead.
  return days.length ? new Set(days) : new Set([1, 2, 3, 4, 5]);
}

export interface SendWindow {
  timezone: string;
  send_window_start: number;
  send_window_end: number;
  send_days: string;
}

export function isWithinSendWindow(date: Date, window: SendWindow): boolean {
  const p = zonedParts(date, window.timezone);
  if (!parseSendDays(window.send_days).has(p.weekday)) return false;
  return p.hour >= window.send_window_start && p.hour < window.send_window_end;
}

/**
 * The next instant at or after `from` that falls inside the send window.
 *
 * `jitterMinutes` spreads sends across the hour instead of firing them all on
 * the same tick — a column of messages timestamped :00:00 is a machine
 * signature, and this is meant to read as a person writing.
 */
export function nextWindowSlot(from: Date, window: SendWindow, jitterMinutes = 0): Date {
  const days = parseSendDays(window.send_days);
  let cursor = new Date(from.getTime());

  for (let attempt = 0; attempt < 400; attempt++) {
    const p = zonedParts(cursor, window.timezone);

    if (!days.has(p.weekday)) {
      cursor = startOfNextZonedDay(cursor, window.timezone);
      continue;
    }
    if (p.hour < window.send_window_start) {
      cursor = zonedTimeToUtc(p.year, p.month, p.day, window.send_window_start, 0, window.timezone);
    } else if (p.hour >= window.send_window_end) {
      cursor = startOfNextZonedDay(cursor, window.timezone);
      continue;
    }

    if (jitterMinutes > 0) {
      const jittered = new Date(cursor.getTime() + Math.floor(Math.random() * jitterMinutes) * 60_000);
      // Only keep the jitter if it did not push us out of the window.
      if (isWithinSendWindow(jittered, window)) return jittered;
    }
    return cursor;
  }

  // Unreachable for any sane config; loud rather than an infinite loop.
  throw new Error(
    `Could not find a send slot within 400 days for timezone=${window.timezone} ` +
      `days=${window.send_days} window=${window.send_window_start}-${window.send_window_end}`,
  );
}

function startOfNextZonedDay(date: Date, timeZone: string): Date {
  const p = zonedParts(date, timeZone);
  // Build local midnight, then add 25h and re-truncate. Going through a
  // >24h jump makes this correct across DST transitions, where a local day
  // can be 23 or 25 hours long.
  const localMidnight = zonedTimeToUtc(p.year, p.month, p.day, 0, 0, timeZone);
  const nextish = new Date(localMidnight.getTime() + 25 * 60 * 60 * 1000);
  const q = zonedParts(nextish, timeZone);
  return zonedTimeToUtc(q.year, q.month, q.day, 0, 0, timeZone);
}

/**
 * Advance `count` sending days (days present in `send_days`), then land inside
 * the window. Used for step delays: "3 days later" should mean three days the
 * campaign actually sends on, not three calendar days that might all be a
 * holiday weekend.
 */
export function addSendingDays(from: Date, count: number, window: SendWindow, jitterMinutes = 0): Date {
  const days = parseSendDays(window.send_days);
  let cursor = from;
  let remaining = count;

  while (remaining > 0) {
    cursor = startOfNextZonedDay(cursor, window.timezone);
    if (days.has(zonedParts(cursor, window.timezone).weekday)) remaining--;
  }

  return nextWindowSlot(cursor, window, jitterMinutes);
}

// ── Display helpers ───────────────────────────────────────────────────────

export function formatRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "—";

  const deltaSeconds = Math.round((then - now) / 1000);
  const past = deltaSeconds < 0;
  const abs = Math.abs(deltaSeconds);

  const units: [number, string][] = [
    [60, "s"],
    [3600, "m"],
    [86400, "h"],
    [86400 * 30, "d"],
  ];

  let text: string;
  if (abs < 45) text = "just now";
  else if (abs < units[1][0]) text = `${Math.round(abs / 60)}m`;
  else if (abs < units[2][0]) text = `${Math.round(abs / 3600)}h`;
  else if (abs < units[3][0]) text = `${Math.round(abs / 86400)}d`;
  else text = `${Math.round(abs / (86400 * 30))}mo`;

  if (text === "just now") return text;
  return past ? `${text} ago` : `in ${text}`;
}

export function formatDateTime(iso: string | null | undefined, timeZone = "America/Los_Angeles"): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(date);
}

/** Inclusive list of 'YYYY-MM-DD' keys ending today — the x-axis for trend charts. */
export function lastNDays(n: number, timeZone = "UTC", end = new Date()): string[] {
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    out.push(dayKey(new Date(end.getTime() - i * 86400_000), timeZone));
  }
  return out;
}
