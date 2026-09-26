import assert from "node:assert/strict";
import test from "node:test";

import {
  checkoutBanner,
  checkoutFailureMessage,
  isPaying,
  isStripeUrl,
  planLabel,
  planTerms,
  portalFailureMessage,
  subscriptionSummary,
  type Subscription,
} from "./billing";

const NOW = new Date("2026-09-26T12:00:00Z");

function sub(over: Partial<Subscription> = {}): Subscription {
  return {
    plan: "starter",
    status: "active",
    current_period_end: "2026-10-26T12:00:00Z",
    included_credits: 1000,
    has_billing_account: true,
    ...over,
  };
}

test("a tier beside a dead status is not paying, matching the API's credit gate", () => {
  assert.equal(isPaying(sub()), true);
  assert.equal(isPaying(sub({ status: "past_due" })), true);
  // An operator-set invoiced tier carries no provider status.
  assert.equal(isPaying(sub({ status: "" })), true);
  assert.equal(isPaying(sub({ status: "canceled" })), false);
  assert.equal(isPaying(sub({ plan: "" })), false);
  assert.equal(isPaying(null), false);
});

test("an unsubscribed organisation is told its signup credits are a budget", () => {
  assert.match(subscriptionSummary(sub({ plan: "", status: "" }), NOW), /signup credits/);
});

test("a past-due subscription says runs keep working while the card is retried", () => {
  // The API keeps serving past_due; telling the customer otherwise would send them to support.
  assert.match(subscriptionSummary(sub({ status: "past_due" }), NOW), /keep working/);
});

test("an active subscription names its renewal date, and a stale date is not shown", () => {
  assert.match(subscriptionSummary(sub(), NOW), /^Renews on /);
  assert.equal(
    subscriptionSummary(sub({ current_period_end: "2026-01-01T00:00:00Z" }), NOW),
    "Active.",
  );
});

test("the success banner does not claim the plan is active before the webhook lands", () => {
  const banner = checkoutBanner("success");
  assert.equal(banner?.tone, "ok");
  // Nor that payment has arrived: a delayed payment method completes checkout before it settles.
  assert.match(banner?.text ?? "", /once Stripe confirms/);
  assert.doesNotMatch(banner?.text ?? "", /Payment received/);
  assert.match(checkoutBanner("cancelled")?.text ?? "", /Nothing was charged/);
  assert.equal(checkoutBanner(undefined), null);
  assert.equal(checkoutBanner("<script>"), null);
});

test("each checkout refusal the API documents has its own message", () => {
  const messages = [503, 400, 409, 500].map((status) => checkoutFailureMessage(status));
  assert.equal(new Set(messages).size, messages.length);
  assert.match(portalFailureMessage(409), /choose a plan first/);
});

test("only Stripe's hosted pages are followed", () => {
  assert.equal(isStripeUrl("https://checkout.stripe.com/c/pay/cs_test_123"), true);
  assert.equal(isStripeUrl("https://billing.stripe.com/p/session/abc"), true);
  assert.equal(isStripeUrl("http://checkout.stripe.com/c/pay"), false);
  assert.equal(isStripeUrl("https://checkout.stripe.com.evil.example/"), false);
  assert.equal(isStripeUrl("javascript:alert(1)"), false);
  assert.equal(isStripeUrl("not a url"), false);
});

test("plan names are shown capitalised, and none reads as 'No plan'", () => {
  assert.equal(planLabel("team"), "Team");
  assert.equal(planLabel(""), "No plan");
});

test("a second checkout refused as already-subscribed points to the portal", () => {
  assert.match(
    checkoutFailureMessage(409, "this organisation already has a subscription; change plans under Manage billing"),
    /Manage billing/,
  );
  assert.match(checkoutFailureMessage(409, "this caller has no organisation to bill"), /no organisation/);
});

test("overage billing is promised only on a tier with a metered price", () => {
  const plan = {
    name: "starter", max_jobs_per_owner: 50, max_concurrent_jobs: 3, rate_limit_per_minute: 240,
    included_credits: 1000, purchasable: true,
  };
  assert.match(planTerms({ ...plan, overage_billed: true }), /billed at the end of the month/);
  assert.doesNotMatch(planTerms({ ...plan, overage_billed: false }), /billed/);
  assert.match(planTerms({ ...plan, purchasable: false }), /Invoiced/);
});
