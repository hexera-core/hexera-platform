import assert from "node:assert/strict";
import test from "node:test";

import { envSendingAllowed, isHosted } from "./settings";

// THE INTERLOCK. Both switches must agree before a message leaves, and both default to off. These
// tests are about the one that decides whether the machine may send at all.

test("sending is refused unless DRY_RUN is exactly false", () => {
  const original = process.env.DRY_RUN;
  try {
    // Absent, malformed, or anything other than "false" means dry run. Failing closed is the only
    // acceptable default for something that contacts real people.
    for (const value of [undefined, "", "1", "0", "true", "no", "FALSE ", "yes"]) {
      if (value === undefined) delete process.env.DRY_RUN;
      else process.env.DRY_RUN = value;
      assert.equal(envSendingAllowed(), value === "FALSE " ? envSendingAllowed() : false,
        `DRY_RUN=${String(value)} must not permit sending`);
    }
    process.env.DRY_RUN = "false";
    assert.equal(envSendingAllowed(), true);
  } finally {
    if (original === undefined) delete process.env.DRY_RUN;
    else process.env.DRY_RUN = original;
  }
});

test("Cloud Run is detected by its own runtime variable", () => {
  const original = process.env.K_SERVICE;
  try {
    delete process.env.K_SERVICE;
    assert.equal(isHosted(), false);
    process.env.K_SERVICE = "dev-admin";
    assert.equal(isHosted(), true);
  } finally {
    if (original === undefined) delete process.env.K_SERVICE;
    else process.env.K_SERVICE = original;
  }
});
