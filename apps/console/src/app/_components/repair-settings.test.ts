import assert from "node:assert/strict";
import test from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AUTO_LABEL, repairSaveFailure, scopeLine } from "./repair-settings";
import { RepairSettingsView } from "./repair-settings-view";

test("the switch says what it does and is labelled for a screen reader", () => {
  const html = renderToStaticMarkup(
    createElement(RepairSettingsView, {
      busy: false,
      error: null,
      on: false,
      saved: { auto_accept_repairs: false, can_change: true },
    }),
  );
  assert.ok(html.includes(AUTO_LABEL));
  assert.ok(html.includes('for="repair-auto-accept"') && html.includes('id="repair-auto-accept"'));
  assert.ok(!html.includes("disabled"), "an admin's switch is not disabled");
});

test("a member who is not an admin sees the setting but cannot change it", () => {
  const html = renderToStaticMarkup(
    createElement(RepairSettingsView, {
      busy: false,
      error: null,
      on: true,
      saved: { auto_accept_repairs: true, can_change: false },
    }),
  );
  assert.ok(html.includes("disabled"));
  assert.ok(html.includes("Only an organisation admin can change this."));
});

test("the line under the switch follows the setting", () => {
  assert.match(scopeLine({ auto_accept_repairs: true }), /may choose to skip the review/);
  assert.match(scopeLine({ auto_accept_repairs: false }), /shows larger repairs for review/);
});

test("a failed save is said in plain words", () => {
  assert.equal(repairSaveFailure(403), "Only an organisation admin can change this.");
  assert.equal(repairSaveFailure(500), "Could not save just now. Try again.");
});
