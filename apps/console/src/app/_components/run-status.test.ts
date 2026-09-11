import assert from "node:assert/strict";
import test from "node:test";

import { formatDuration, statusClass } from "./run-status";

test("statusClass maps each terminal state to its own mark", () => {
  assert.equal(statusClass("succeeded"), "status status--ok");
  assert.equal(statusClass("failed"), "status status--fail");
  assert.equal(statusClass("running"), "status status--run");
});

test("statusClass falls back to the neutral mark for a state it does not know", () => {
  // JobStatus can gain a member without this file changing. An unknown state must render as a
  // neutral row, never crash the table.
  assert.equal(statusClass("some_future_state"), "status");
  assert.equal(statusClass(""), "status");
});

test("formatDuration reports a finished run in whole units", () => {
  assert.equal(formatDuration("2026-09-10T09:00:00Z", "2026-09-10T09:04:30Z"), "4m 30s");
  assert.equal(formatDuration("2026-09-10T09:00:00Z", "2026-09-10T11:30:00Z"), "2h 30m");
  assert.equal(formatDuration("2026-09-10T09:00:00Z", "2026-09-10T09:00:12Z"), "12s");
});

test("formatDuration reports an unfinished or unstarted run as an em dash", () => {
  assert.equal(formatDuration("2026-09-10T09:00:00Z", null), "—");
  assert.equal(formatDuration(null, "2026-09-10T09:00:00Z"), "—");
  assert.equal(formatDuration(null, null), "—");
});

test("formatDuration does not report a negative duration", () => {
  // Clock skew between the API host and the worker can end a job before it starts on paper.
  assert.equal(formatDuration("2026-09-10T09:00:10Z", "2026-09-10T09:00:00Z"), "—");
});
