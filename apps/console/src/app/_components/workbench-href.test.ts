import assert from "node:assert/strict";
import { test } from "node:test";

import { isWorkbenchHref } from "./workbench-href";

test("run pages and New run are workbench links", () => {
  assert.equal(isWorkbenchHref("/runs/new"), true);
  assert.equal(isWorkbenchHref("/runs/0f3a-job"), true);
  assert.equal(isWorkbenchHref("/runs/0f3a-job?tab=files"), true);
});

test("lists and other sections are not", () => {
  assert.equal(isWorkbenchHref("/runs"), false);
  assert.equal(isWorkbenchHref("/runs?cursor=abc"), false);
  assert.equal(isWorkbenchHref("/"), false);
  assert.equal(isWorkbenchHref("/conversations/123"), false);
  assert.equal(isWorkbenchHref("/runs/a/b"), false);
});
