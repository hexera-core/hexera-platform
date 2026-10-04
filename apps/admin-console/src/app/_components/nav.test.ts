import assert from "node:assert/strict";
import { test } from "node:test";

import { buildAdminSections, isLaunchAdminSurfaceEnabled } from "./sections";

test("the shell offers the admin sections in a fixed order", () => {
  assert.deepEqual(
    buildAdminSections({ ENV: "prod", OUTREACH_ENABLED: "1" }).map((s) => s.label),
    ["Fleet", "Costs", "Billing", "Activity", "Customers", "Repair", "Outreach"],
  );
});

test("every section has a route under /", () => {
  for (const section of buildAdminSections({ ENV: "prod", OUTREACH_ENABLED: "1" })) {
    assert.match(section.href, /^\/[a-z-]*$/, `bad href for ${section.label}`);
  }
});

test("dev navigation keeps launch-only surfaces out of the way", () => {
  assert.deepEqual(
    buildAdminSections({ ENV: "dev" }).map((s) => s.label),
    // Repair is NOT launch-only: a personal environment is where the repair loop is exercised,
    // so the queue has to be openable there.
    ["Fleet", "Costs", "Billing", "Repair"],
  );
});

test("prod navigation unblocks launch operator surfaces", () => {
  assert.deepEqual(
    buildAdminSections({ ENV: "prod" }).map((s) => [s.label, s.available]),
    [
      ["Fleet", true],
      ["Costs", true],
      ["Billing", true],
      ["Activity", true],
      ["Customers", true],
      ["Repair", true],
    ],
  );
});

test("launch-only pages render only for the prod app environment", () => {
  assert.equal(isLaunchAdminSurfaceEnabled({ ENV: "prod" }), true);
  assert.equal(isLaunchAdminSurfaceEnabled({ APP_ENV: "prod" }), true);
  assert.equal(isLaunchAdminSurfaceEnabled({ ENV: "dev", APP_ENV: "prod" }), false);
  assert.equal(isLaunchAdminSurfaceEnabled({ ENV: "production" }), false);
  assert.equal(isLaunchAdminSurfaceEnabled({}), false);
});

test("Outreach appears only where the deployment says it runs", () => {
  const labelled = (env: Record<string, string | undefined>) =>
    buildAdminSections(env).find((s) => s.label === "Outreach")?.available;

  assert.equal(labelled({ OUTREACH_ENABLED: "1" }), true);
  assert.equal(labelled({}), undefined);
  // Anything other than the exact flag keeps it dark - "0" and "true" are both refusals, because
  // the deploy workflow sets "1" or leaves it empty and nothing else is a value it produces.
  assert.equal(labelled({ OUTREACH_ENABLED: "0" }), undefined);
  assert.equal(labelled({ OUTREACH_ENABLED: "true" }), undefined);
});

test("hrefs are unique", () => {
  const hrefs = buildAdminSections({ ENV: "prod", OUTREACH_ENABLED: "1" }).map((s) => s.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});
