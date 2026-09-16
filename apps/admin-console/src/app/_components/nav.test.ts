import assert from "node:assert/strict";
import { test } from "node:test";

import { ADMIN_SECTIONS, buildAdminSections } from "./sections";

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
  // is available with no data at all, because its page explains its own emptiness; Activity and
  // Customers have no page at all.
  const unavailable = buildAdminSections({}).filter((s) => !s.available).map((s) => s.label);
  assert.deepEqual(unavailable, ["Activity", "Customers", "Outreach"]);
});

test("Outreach appears only where the deployment says it runs", () => {
  const labelled = (env: Record<string, string | undefined>) =>
    buildAdminSections(env).find((s) => s.label === "Outreach")?.available;

  assert.equal(labelled({ OUTREACH_ENABLED: "1" }), true);
  assert.equal(labelled({}), false);
  // Anything other than the exact flag keeps it dark - "0" and "true" are both refusals, because
  // the deploy workflow sets "1" or leaves it empty and nothing else is a value it produces.
  assert.equal(labelled({ OUTREACH_ENABLED: "0" }), false);
  assert.equal(labelled({ OUTREACH_ENABLED: "true" }), false);
});

test("hrefs are unique", () => {
  const hrefs = ADMIN_SECTIONS.map((s) => s.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});
