import assert from "node:assert/strict";
import { test } from "node:test";

import { ADMIN_SECTIONS } from "./sections";

test("the shell offers the five admin sections in a fixed order", () => {
  assert.deepEqual(
    ADMIN_SECTIONS.map((s) => s.label),
    ["Fleet", "Costs", "Activity", "Customers", "Outreach"],
  );
});

test("every section has a route under /", () => {
  for (const section of ADMIN_SECTIONS) {
    assert.match(section.href, /^\/[a-z-]*$/, `bad href for ${section.label}`);
  }
});

test("sections whose data does not exist yet are marked unavailable", () => {
  // Customers reads accounts and metering, which do not exist; Outreach lives only in prod.
  // Marking them unavailable is what stops the shell linking to a page that cannot render.
  const unavailable = ADMIN_SECTIONS.filter((s) => !s.available).map((s) => s.label);
  assert.deepEqual(unavailable, ["Customers", "Outreach"]);
});

test("hrefs are unique", () => {
  const hrefs = ADMIN_SECTIONS.map((s) => s.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});
