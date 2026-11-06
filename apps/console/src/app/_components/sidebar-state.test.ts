import assert from "node:assert/strict";
import test from "node:test";

import { SIDEBAR_COOKIE, sidebarCookie, sidebarHidden } from "./sidebar-state";

test("the sidebar is shown unless the user hid it", () => {
  assert.equal(sidebarHidden(undefined), false);
  assert.equal(sidebarHidden(null), false);
  assert.equal(sidebarHidden("shown"), false);
  assert.equal(sidebarHidden("rail"), false);
  assert.equal(sidebarHidden("hidden"), true);
});

test("the cookie round-trips through the reader", () => {
  for (const hidden of [true, false]) {
    const cookie = sidebarCookie(hidden);
    assert.ok(cookie.startsWith(`${SIDEBAR_COOKIE}=`));
    assert.match(cookie, /path=\//);
    const value = cookie.split(";")[0].split("=")[1];
    assert.equal(sidebarHidden(value), hidden);
  }
});
