import assert from "node:assert/strict";
import test from "node:test";

import { formatSigned } from "./ledger";

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
