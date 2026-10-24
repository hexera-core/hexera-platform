import assert from "node:assert/strict";
import test from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { DashboardNotice } from "./dashboard-notice";

test("an error notice identifies the problem and offers a recovery action", () => {
  const html = renderToStaticMarkup(
    createElement(DashboardNotice, {
      action: { href: "/runs", label: "Return to runs" },
      body: "Refresh the page to try again.",
      title: "We couldn't load this run",
      tone: "error",
    }),
  );

  assert.match(html, /role="alert"/);
  assert.match(html, /<h2[^>]*>We couldn&#x27;t load this run<\/h2>/);
  assert.match(html, /Refresh the page to try again\./);
  assert.match(html, /href="\/runs"[^>]*>Return to runs<\/a>/);
});

test("an empty notice stays informational and can point to the next step", () => {
  const html = renderToStaticMarkup(
    createElement(DashboardNotice, {
      action: { href: "/runs/new", label: "Start a run" },
      body: "Upload a geometry file and describe what the mesh is for.",
      title: "No runs yet",
    }),
  );

  assert.doesNotMatch(html, /role="alert"/);
  assert.match(html, /<h2[^>]*>No runs yet<\/h2>/);
  assert.match(html, /href="\/runs\/new"[^>]*>Start a run<\/a>/);
});
