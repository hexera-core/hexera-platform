import assert from "node:assert/strict";
import test from "node:test";

import { ageWords, recentRow, runDot } from "./recent-runs";

const NOW = Date.parse("2026-10-28T12:00:00Z");

test("a finished run is marked by how it ended", () => {
  assert.equal(runDot("succeeded"), "ok");
  assert.equal(runDot("succeeded", "delivered_with_concerns"), "warn");
  assert.equal(runDot("failed"), "bad");
});

test("a run that is still moving pulses, and an unknown state stays plain", () => {
  for (const status of ["running", "pending", "queued", "pending_review"]) {
    assert.equal(runDot(status), "live", status);
  }
  assert.equal(runDot("cancelled"), "");
  assert.equal(runDot("something_new"), "");
});

test("the age is short words, never negative", () => {
  assert.equal(ageWords("2026-10-28T11:59:30Z", NOW), "now");
  assert.equal(ageWords("2026-10-28T11:48:00Z", NOW), "12 min");
  assert.equal(ageWords("2026-10-28T09:00:00Z", NOW), "3 h");
  assert.equal(ageWords("2026-10-26T12:00:00Z", NOW), "2 d");
  assert.equal(ageWords("2026-09-28T12:00:00Z", NOW), "4 w");
  // a worker clock a little ahead still reads "now"; far ahead reads as nothing
  assert.equal(ageWords("2026-10-28T12:00:30Z", NOW), "now");
  assert.equal(ageWords("2026-10-28T13:00:00Z", NOW), "");
  assert.equal(ageWords(null, NOW), "");
  assert.equal(ageWords("not a date", NOW), "");
  // before the browser has a clock, no age at all
  assert.equal(ageWords("2026-10-28T11:48:00Z", Number.NaN), "");
});

test("a row links to its run and never shows an empty title", () => {
  const row = recentRow({ id: "a b", status: "running", task_label: null, created_at: null }, NOW);
  assert.equal(row.href, "/runs/a%20b");
  assert.equal(row.title, "Untitled run");
  assert.equal(row.dot, "live");
  assert.equal(row.age, "");
  assert.equal(row.words, "Running");
  const rerun = recentRow({ id: "x", status: "succeeded", task_label: null, is_rerun: true, created_at: null }, NOW);
  assert.equal(rerun.title, "Re-review of an earlier run");
});
