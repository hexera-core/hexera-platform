import assert from "node:assert/strict";
import test from "node:test";

import { CONSOLE_SECTIONS, currentSection } from "./sections";

test("every section has a unique href", () => {
  const hrefs = CONSOLE_SECTIONS.map((section) => section.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
});

test("the product group leads and the account group follows", () => {
  const groups = CONSOLE_SECTIONS.map((section) => section.group);
  const firstAccount = groups.indexOf("account");
  assert.ok(firstAccount > 0, "there should be product sections before the account ones");
  assert.ok(
    groups.slice(firstAccount).every((group) => group === "account"),
    "the account group must not be interleaved with the product one",
  );
});

test("currentSection matches a nested path to the section that owns it", () => {
  // /runs/<uuid> and /runs/new both belong to /runs, or the sidebar loses its mark the moment
  // somebody opens a run.
  assert.equal(currentSection("/runs/8f14e45f-ceea-467a-9b1e-2c1d0e4a1b22"), "/runs");
  assert.equal(currentSection("/runs/new"), "/runs");
  assert.equal(currentSection("/settings/api-keys"), "/settings/api-keys");
  assert.equal(currentSection("/conversations/abc"), "/conversations");
});

test("currentSection matches the overview only on the root itself", () => {
  // "/" is a prefix of every path; a naive startsWith would mark the overview current forever.
  assert.equal(currentSection("/"), "/");
  assert.notEqual(currentSection("/usage"), "/");
});

test("currentSection returns an empty string for a path no section owns", () => {
  assert.equal(currentSection("/sign-in"), "");
});
