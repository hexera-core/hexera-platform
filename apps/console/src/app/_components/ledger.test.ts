import assert from "node:assert/strict";
import test from "node:test";

import { formatCredits, formatSigned, ledgerReason, shortRunId } from "./ledger";

test("a grant reads as an explicit credit", () => {
  // The column is SIGNED in the database so a client sums it rather than branching on the type.
  // A bare "500" beside a "-20" reads as though only one of them has a direction.
  assert.equal(formatSigned(500), "+500");
});

test("a debit keeps its own sign rather than gaining a second one", () => {
  assert.equal(formatSigned(-20), "−20");
});

test("zero carries no sign", () => {
  assert.equal(formatSigned(0), "0");
});

test("large amounts are grouped", () => {
  assert.equal(formatSigned(-12787), "−12,787");
  assert.equal(formatSigned(1000), "+1,000");
});

test("a negative balance shows a true minus and grouping, never a raw -12787", () => {
  assert.equal(formatCredits(-12787), "−12,787");
  assert.equal(formatCredits(4500), "4,500");
  assert.equal(formatCredits(0), "0");
});

test("a run charge names the run, not the raw reason", () => {
  const id = "5c2e43fe-9a1b-4c2d-8e3f-00174876e800";
  assert.deepEqual(ledgerReason(`mesh job ${id}`), { text: "", runId: id });
  assert.deepEqual(ledgerReason(`billed as overage: ${id}`), { text: "Billed as overage", runId: id });
  assert.equal(shortRunId(id), "5c2e43fe");
});

test("any other reason is shown as written", () => {
  assert.deepEqual(ledgerReason("signup grant"), { text: "Signup grant", runId: null });
  assert.deepEqual(ledgerReason(""), { text: "", runId: null });
});

test("a uuid inside a free-text grant is not mistaken for a run", () => {
  // Admin credits carry written reasons; "Adjustment for invoice <uuid>" must stay words, not
  // become a link to a run that does not exist.
  const reason = "Adjustment for invoice 8f14e45f-ceea-467a-9b1e-2c1d0e4a1b22";
  assert.deepEqual(ledgerReason(reason), { text: reason, runId: null });
});

test("a uuid inside a free-text grant is not mistaken for a run", () => {
  // Admin credits carry written reasons; "Adjustment for invoice <uuid>" must stay words, not
  // become a link to a run that does not exist.
  const reason = "Adjustment for invoice 8f14e45f-ceea-467a-9b1e-2c1d0e4a1b22";
  assert.deepEqual(ledgerReason(reason), { label: reason, runId: null });
  assert.equal(ledgerReason("mesh job not-a-uuid").runId, null);
});
