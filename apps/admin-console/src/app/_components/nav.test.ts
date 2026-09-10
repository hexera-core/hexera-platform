import assert from "node:assert/strict";
import { test } from "node:test";

import { ADMIN_SECTIONS } from "./sections";

test("the shell offers the admin sections in a fixed order", () => {
  assert.deepEqual(
    ADMIN_SECTIONS.map((s) => s.label),
    ["Fleet", "Costs", "Billing", "Activity", "Customers", "Outreach"],
  );
});

test("every section has a route under /", () => {
  for (const section of ADMIN_SECTIONS) {
    assert.match(section.href, /^\/[a-z-]*$/, `bad href for ${section.label}`);
  }
});

test("sections with no page behind them are marked unavailable", () => {
  // Availability tracks whether the LINK renders something, not whether the data exists. Billing
  // is available with no data at all, because its page explains its own emptiness; Activity,
  // Customers and Outreach have no page in this deployment.
  const unavailable = ADMIN_SECTIONS.filter((s) => !s.available).map((s) => s.label);
  assert.deepEqual(unavailable, ["Activity", "Customers", "Outreach"]);
});

test("hrefs are unique", () => {
  const hrefs = ADMIN_SECTIONS.map((s) => s.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});
