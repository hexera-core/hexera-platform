import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const sources = [
  "(admin)/outreach/analytics/page.tsx",
  "(admin)/outreach/inbox/page.tsx",
  "(admin)/outreach/partners/page.tsx",
  "(admin)/outreach/queue/page.tsx",
  "(admin)/outreach/settings/page.tsx",
].map((path) => readFileSync(join(process.cwd(), "src/app", path), "utf8"));

test("outreach UI omits implementation narration and obvious interaction mechanics", () => {
  const copy = sources.join("\n");
  const lowValuePhrases = [
    "Filled is contacted. The rest is still to do.",
    "Pick a breakdown and a measure.",
    "The worker reads the connected mailbox each tick",
    "Previews come from the live templates and the real contact record",
    "Tick the ones you want, then send them or take them back out of the campaign.",
    "The worker keeps verifying addresses and reading replies",
    "You cannot send from this page, on purpose.",
  ];

  for (const phrase of lowValuePhrases) {
    assert.equal(copy.includes(phrase), false, `remove or rewrite low-value copy: ${phrase}`);
  }

  assert.match(copy, /Solid shows contacted; outlined shows remaining\./);
  assert.match(
    sources[3],
    /Daily caps, one person per company per day, and both safety switches still apply\./,
  );
  assert.match(
    sources[2],
    /YC companies must be selected manually; enrollment requires a verified email\./,
  );
});
