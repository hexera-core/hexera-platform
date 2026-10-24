import assert from "node:assert/strict";
import test from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import DashboardError from "../(dashboard)/error";
import DashboardLoading from "../(dashboard)/loading";

test("dashboard navigation announces that content is loading", () => {
  const html = renderToStaticMarkup(createElement(DashboardLoading));

  assert.match(html, /role="status"/);
  assert.doesNotMatch(html, /aria-busy/);
  assert.match(html, /Loading workspace/);
  assert.match(html, /Fetching the latest data/);
});

test("an unexpected dashboard error gives the user a retry control", () => {
  const html = renderToStaticMarkup(
    createElement(DashboardError, {
      error: new Error("sensitive implementation detail"),
      reset: () => undefined,
    }),
  );

  assert.match(html, /role="alert"/);
  assert.match(html, /We couldn&#x27;t open this page/);
  assert.match(html, /<button[^>]*>Try again<\/button>/);
  assert.match(html, /href="\/"[^>]*>Return to overview<\/a>/);
  assert.doesNotMatch(html, /sensitive implementation detail/);
});
