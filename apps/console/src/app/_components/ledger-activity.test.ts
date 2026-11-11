import assert from "node:assert/strict";
import test from "node:test";

import {
  activityTotals,
  activityWindow,
  balanceSeries,
} from "./ledger-activity";

const NOW = new Date("2026-11-10T18:00:00Z");

function entry(amount: number, day: string): { amount: number; created_at: string } {
  return { amount, created_at: `${day}T12:00:00Z` };
}

test("activityWindow buckets signed amounts into UTC days", () => {
  const days = activityWindow(
    [entry(-12, "2026-11-09"), entry(-8, "2026-11-09"), entry(100, "2026-11-10")],
    NOW,
    5,
  );
  assert.equal(days.length, 5);
  assert.deepEqual(
    days.map((d) => [d.day, d.spent, d.granted]),
    [
      ["2026-11-06", 0, 0],
      ["2026-11-07", 0, 0],
      ["2026-11-08", 0, 0],
      ["2026-11-09", 20, 0],
      ["2026-11-10", 0, 100],
    ],
  );
});

test("activityWindow ignores entries outside the window or without a date", () => {
  const days = activityWindow(
    [
      entry(-5, "2026-10-01"),
      { amount: -7, created_at: null },
      { amount: -3, created_at: "not a date" },
    ],
    NOW,
    5,
  );
  assert.deepEqual(activityTotals(days), { spent: 0, granted: 0 });
});

test("balanceSeries fills every day when the window is the whole ledger", () => {
  const days = activityWindow([entry(-10, "2026-11-09"), entry(100, "2026-11-08")], NOW, 5);
  // balance now is 90: end of Nov 10 = 90, Nov 9 = 90 - 0 = 90... walk back through nets.
  const series = balanceSeries(days, 90, true);
  assert.deepEqual(series, [0, 0, 100, 90, 90]);
});

test("balanceSeries leaves days before the first covered entry empty when truncated", () => {
  const days = activityWindow([entry(-10, "2026-11-09"), entry(100, "2026-11-08")], NOW, 5);
  const series = balanceSeries(days, 90, false);
  // Nov 8 holds the earliest entry the truncated read saw; earlier days are unproven.
  assert.deepEqual(series, [null, null, 100, 90, 90]);
});

test("balanceSeries is all empty when a truncated window carries no entries", () => {
  const days = activityWindow([], NOW, 5);
  assert.deepEqual(balanceSeries(days, 90, false), [null, null, null, null, null]);
});
