import assert from "node:assert/strict";
import test from "node:test";

import { credits, money, planLabel, when } from "./read";

test("money divides the provider's smallest unit rather than trusting a decimal", () => {
  // The provider sends cents. Rendering 250000 as $250,000.00 instead of $2,500.00 is the kind of
  // error an operator acts on before anyone notices it was a units mistake.
  assert.equal(money(250_000, "usd"), "$2,500.00");
  assert.equal(money(0, "usd"), "$0.00");
});

test("money accepts the currency in whatever case the provider sent it", () => {
  assert.equal(money(1_000, "USD"), "$10.00");
  assert.equal(money(1_000, ""), "$10.00");
});

test("money falls back rather than throwing on a currency Intl rejects", () => {
  // A page that throws on one unexpected currency loses the four panels that were fine.
  assert.equal(money(1_000, "not-a-currency"), "10.00 NOT-A-CURRENCY");
});

test("credits keep their sign so a grant and a debit never look alike", () => {
  // A ledger reader scans a column. Dropping the sign on grants makes the column unreadable at the
  // exact moment it matters - when a balance is lower than a customer expected.
  assert.equal(credits(1_500), "+1,500");
  assert.equal(credits(-1_500), "-1,500");
  assert.equal(credits(0), "0");
});

test("an empty plan reads as free rather than as missing data", () => {
  // settings/plans.py resolves an empty plan to the deployment's own limits, so blank is the most
  // common and entirely correct state. An empty cell would read as a failed lookup.
  assert.equal(planLabel(""), "free");
  assert.equal(planLabel("   "), "free");
  assert.equal(planLabel("team"), "team");
});

test("when renders a dash for absent or unparseable timestamps", () => {
  // `current_period_end` is null for every organisation that never subscribed, which is most of
  // them; "Invalid Date" in that column would look like a bug in the page rather than a free tier.
  assert.equal(when(null), "—");
  assert.equal(when("not a date"), "—");
  assert.equal(when("2026-09-22T12:00:00+00:00"), "2026-09-22");
});
