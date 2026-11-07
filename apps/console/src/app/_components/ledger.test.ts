import assert from "node:assert/strict";
import test from "node:test";

import { formatSigned, ledgerReason } from "./ledger";

test("a grant reads as an explicit credit", () => {
  // The column is SIGNED in the database so a client sums it rather than branching on the type.
  // A bare "500" beside a "-20" reads as though only one of them has a direction.
  assert.equal(formatSigned(500), "+500");
});

test("a debit keeps its own sign rather than gaining a second one", () => {
  assert.equal(formatSigned(-20), "-20");
});

test("zero carries no sign", () => {
  assert.equal(formatSigned(0), "0");
});

test("a run charge links back to the run that caused it", () => {
  const id = "8f14e45f-ceea-467a-9b1e-2c1d0e4a1b22";
  assert.deepEqual(ledgerReason(`mesh job ${id}`), {
    label: "Mesh run 8f14e45f",
    runId: id,
  });
  assert.deepEqual(ledgerReason(`billed as overage: ${id}`), {
    label: "Billed as overage · run 8f14e45f",
    runId: id,
  });
});

test("a reason without a job id shows as written", () => {
  assert.deepEqual(ledgerReason("signup grant"), { label: "signup grant", runId: null });
  assert.deepEqual(ledgerReason("plan allowance"), { label: "plan allowance", runId: null });
  assert.deepEqual(ledgerReason(""), { label: "—", runId: null });
});
