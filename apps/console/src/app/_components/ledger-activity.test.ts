import assert from "node:assert/strict";
import test from "node:test";

import { CHART_SOURCES, spendByDay, spendTotal } from "./ledger-activity";

const NOW = new Date("2026-11-10T18:00:00Z");

type Entry = { amount: number; created_at: string | null; source: string };

function entry(amount: number, day: string, source = "run-a"): Entry {
  return { amount, created_at: `${day}T12:00:00Z`, source };
}

const sourceOf = (e: Entry) => ({ key: e.source, label: e.source });

test("spendByDay buckets spend into UTC days and ignores grants", () => {
  const { days } = spendByDay(
    [entry(-12, "2026-11-09"), entry(-8, "2026-11-09"), entry(100, "2026-11-10")],
    sourceOf,
    NOW,
    5,
  );
  assert.equal(days.length, 5);
  assert.deepEqual(
    days.map((d) => [d.day, d.total]),
    [
      ["2026-11-06", 0],
      ["2026-11-07", 0],
      ["2026-11-08", 0],
      ["2026-11-09", 20],
      ["2026-11-10", 0],
    ],
  );
});

test("spendByDay skips entries outside the window or without a date", () => {
  const { days, sources } = spendByDay(
    [
      entry(-5, "2026-10-01"),
      { amount: -7, created_at: null, source: "run-a" },
      { amount: -3, created_at: "not a date", source: "run-a" },
    ],
    sourceOf,
    NOW,
    5,
  );
  assert.equal(spendTotal(days), 0);
  assert.deepEqual(sources, []);
});

test("spendByDay splits a day by source and ranks sources by spend", () => {
  const { days, sources } = spendByDay(
    [
      entry(-6, "2026-11-09", "run-a"),
      entry(-4, "2026-11-09", "run-b"),
      entry(-30, "2026-11-10", "run-b"),
    ],
    sourceOf,
    NOW,
    5,
  );
  assert.deepEqual(
    sources.map((s) => [s.key, s.amount]),
    [
      ["run-b", 34],
      ["run-a", 6],
    ],
  );
  const day = days[3];
  assert.equal(day.total, 10);
  // parts follow the ranked source order, not entry order
  assert.deepEqual(
    day.parts.map((p) => [p.key, p.amount]),
    [
      ["run-b", 4],
      ["run-a", 6],
    ],
  );
});

test("spendByDay merges the long tail past CHART_SOURCES into Other", () => {
  const entries = Array.from({ length: CHART_SOURCES + 2 }, (_, i) =>
    entry(-(i + 1), "2026-11-09", `run-${i}`),
  );
  const { days, sources } = spendByDay(entries, sourceOf, NOW, 5);
  assert.equal(sources.length, CHART_SOURCES + 1);
  const other = sources.at(-1);
  assert.equal(other?.key, "other");
  assert.equal(other?.amount, 2 + 1); // the two smallest sources
  const day = days[3];
  assert.equal(day.parts.at(-1)?.key, "other");
  assert.equal(day.total, entries.reduce((sum, e) => sum - e.amount, 0));
});

test("spendByDay keeps the resolver's label for the legend and tooltips", () => {
  const { sources, days } = spendByDay(
    [entry(-9, "2026-11-10", "deadbeef")],
    () => ({ key: "deadbeef", label: "Wing study" }),
    NOW,
    5,
  );
  assert.equal(sources[0].label, "Wing study");
  assert.equal(days.at(-1)?.parts[0].label, "Wing study");
});
