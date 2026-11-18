import assert from "node:assert/strict";
import test from "node:test";

import { formatWhen, fullWhen } from "./when";

const NOW = new Date("2026-11-08T15:00:00Z");

test("today and yesterday read as words with a time", () => {
  assert.equal(formatWhen("2026-11-08T11:42:07Z", NOW, "UTC"), "Today 11:42 AM");
  assert.equal(formatWhen("2026-11-07T20:23:00Z", NOW, "UTC"), "Yesterday 8:23 PM");
});

test("earlier this year is a short date and a time; another year drops the time", () => {
  assert.equal(formatWhen("2026-11-02T20:23:00Z", NOW, "UTC"), "Nov 2, 8:23 PM");
  assert.equal(formatWhen("2025-03-14T09:00:00Z", NOW, "UTC"), "Mar 14, 2025");
});

test("the day boundary is the reader's own", () => {
  // 01:30 UTC on the 8th is still the evening of the 7th in New York
  assert.equal(formatWhen("2026-11-08T01:30:00Z", NOW, "America/New_York"), "Yesterday 8:30 PM");
});

test("a missing or broken time is an em dash", () => {
  assert.equal(formatWhen(null, NOW, "UTC"), "—");
  assert.equal(formatWhen("not a date", NOW, "UTC"), "—");
});

test("the tooltip keeps the full stamp", () => {
  assert.equal(fullWhen("2026-11-07T20:23:07Z", "UTC"), "Nov 7, 2026, 8:23:07 PM");
});
