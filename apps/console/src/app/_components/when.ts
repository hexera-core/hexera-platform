// Dates in lists, in the shortest words that stay exact: "Today 11:42 PM", "Yesterday 8:23 PM",
// "Nov 7, 8:23 PM", and the year only when it is not this one. Pure, so it is testable without a
// clock or a browser; `timeZone` is left out in the browser (the reader's own) and given in tests.

function dayKey(at: Date, timeZone?: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    day: "2-digit", month: "2-digit", timeZone, year: "numeric",
  }).format(at);
}

export function formatWhen(iso: string | null, now: Date, timeZone?: string): string {
  if (!iso) {
    return "—";
  }
  const at = new Date(iso);
  if (!Number.isFinite(at.getTime())) {
    return "—";
  }
  const time = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone })
    .format(at).replace(/ /g, " ");
  const day = dayKey(at, timeZone);
  if (day === dayKey(now, timeZone)) {
    return `Today ${time}`;
  }
  if (day === dayKey(new Date(now.getTime() - 86_400_000), timeZone)) {
    return `Yesterday ${time}`;
  }
  const sameYear = day.slice(0, 4) === dayKey(now, timeZone).slice(0, 4);
  const date = new Intl.DateTimeFormat("en-US", {
    day: "numeric", month: "short", timeZone, ...(sameYear ? {} : { year: "numeric" }),
  }).format(at);
  return sameYear ? `${date}, ${time}` : date;
}

/** The full stamp for a tooltip: "Nov 7, 2026, 8:23:07 PM". */
export function fullWhen(iso: string, timeZone?: string): string {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium", timeStyle: "medium", timeZone,
  }).format(new Date(iso)).replace(/ /g, " ");
}
