import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const sources = [
  "(admin)/activity/page.tsx",
  "(admin)/billing/page.tsx",
  "(admin)/customers/page.tsx",
  "(admin)/customers/[organizationId]/page.tsx",
  "(admin)/repair/page.tsx",
  "(admin)/jobs/[jobId]/why/page.tsx",
].map((path) => readFileSync(join(process.cwd(), "src/app", path), "utf8"));

const repairDetail = readFileSync(
  join(process.cwd(), "src/app/(admin)/repair/[jobId]/page.tsx"),
  "utf8",
);

test("core operational pages omit redundant and implementation-oriented microcopy", () => {
  const copy = sources.join("\n");
  const lowValuePhrases = [
    "Recent runs across every organisation, newest first.",
    "cross-tenant, newest first",
    "settings/plans.py",
    "derived from credit_ledger, never stored",
    "derived from organisations, jobs and credit_ledger",
    "click a heading to sort",
    "Organisation detail, recent runs, invoices and append-only credit controls.",
    "counted from the same rows this queue is worked from",
    "one sentence per decision",
  ];

  for (const phrase of lowValuePhrases) {
    assert.equal(copy.includes(phrase), false, `remove or rewrite low-value copy: ${phrase}`);
  }
});

test("repair detail presents one CAD Repair System rather than internal repair modes", () => {
  assert.match(repairDetail, /CAD Repair System/);
  assert.equal(repairDetail.includes("<th>Mode</th>"), false);
});
