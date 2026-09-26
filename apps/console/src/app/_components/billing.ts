// What the billing page says, kept out of the components so it can be tested without a DOM.
//
// Every shape here mirrors src/meshpipeline/api/v1/billing.py. Prices are NOT among them: the API
// deliberately does not report what a tier costs (the provider is the one place a price can change
// without a deploy), so the page names tiers and what they include, and Stripe's own checkout page
// shows the amount.

export type Plan = {
  name: string;
  max_jobs_per_owner: number;
  max_concurrent_jobs: number;
  rate_limit_per_minute: number;
  included_credits: number;
  purchasable: boolean;
  /** whether use beyond the allowance is billed; absent from an API older than this field */
  overage_billed?: boolean;
};

export type Catalogue = { plans: Plan[]; billing_enabled: boolean };

export type Subscription = {
  plan: string;
  status: string;
  current_period_end: string | null;
  included_credits: number;
  has_billing_account: boolean;
};

/** Where a tier that cannot be checked out sends people. CONFIRM THIS MAILBOX EXISTS before launch. */
export const SALES_CONTACT = "mailto:sales@hexera.ai";

/** The subscription states that still pay (mirrors application/spend_gate.PAYING_STATUSES). */
const PAYING = new Set(["active", "trialing", "past_due", ""]);

export function isPaying(subscription: Subscription | null): boolean {
  return Boolean(subscription?.plan) && PAYING.has(subscription?.status ?? "");
}

export function planLabel(name: string): string {
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : "No plan";
}

/** One line under the plan name: what state it is in, in words a customer would use. */
export function subscriptionSummary(subscription: Subscription | null, now = new Date()): string {
  if (!subscription || !isPaying(subscription)) {
    return "You are running on your signup credits. When they run out, new runs need a plan.";
  }
  const end = subscription.current_period_end ? new Date(subscription.current_period_end) : null;
  const date = end && end > now ? end.toLocaleDateString() : null;
  if (subscription.status === "past_due") {
    return "Your last payment did not go through. Update your card under Manage billing - runs keep working while Stripe retries.";
  }
  if (subscription.status === "trialing") {
    return date ? `Trial - the first charge is on ${date}.` : "Trial.";
  }
  return date ? `Renews on ${date}.` : "Active.";
}

/** The banner Stripe's redirect asks for, via ?checkout=success|cancelled; null for anything else. */
export function checkoutBanner(param: string | undefined): { tone: "ok" | "info"; text: string } | null {
  if (param === "success") {
    // FULFILMENT IS THE WEBHOOK'S, not this redirect's - it may land a few seconds after the
    // browser does, so the page must not claim the plan is already active.
    // NOR THAT THE MONEY HAS ARRIVED: with a delayed payment method checkout completes before
    // the payment settles.
    return {
      tone: "ok",
      text: "Checkout complete. Your plan and its credits appear here once Stripe confirms the payment - usually within a minute.",
    };
  }
  if (param === "cancelled") {
    return { tone: "info", text: "Checkout cancelled. Nothing was charged." };
  }
  return null;
}

/** POST /billing/checkout failed with `status`; `detail` is the API's own reason, when it gave one. */
export function checkoutFailureMessage(status: number, detail = ""): string {
  if (status === 503) {
    return "Billing is not switched on for this deployment yet.";
  }
  if (status === 400) {
    return "That plan cannot be bought here. Choose another, or contact us.";
  }
  if (status === 409 && /already has a subscription/.test(detail)) {
    // The API refuses a second checkout: it would be a second subscription, not a plan change.
    return "You already have a plan. Change it under Manage billing.";
  }
  if (status === 409) {
    return "Your account has no organisation to bill yet. Sign out and back in, then try again.";
  }
  return "Could not start checkout just now. Try again.";
}

/** What a tier's card says about use beyond its allowance - promised only where it is billed. */
export function planTerms(plan: Plan): string {
  const limits = `Up to ${plan.max_jobs_per_owner} active runs, ${plan.rate_limit_per_minute} API requests a minute.`;
  if (!plan.purchasable) {
    return `${limits} Invoiced - usage beyond the included credits is agreed with us.`;
  }
  // NOTHING IS PROMISED for a sold tier without a metered price: that is a deployment that has
  // not configured one, and neither "billed" nor "stops" would be true of it.
  return plan.overage_billed
    ? `${limits} Usage beyond the included credits is billed at the end of the month.`
    : limits;
}

/** POST /billing/portal failed with `status`. */
export function portalFailureMessage(status: number): string {
  if (status === 503) {
    return "Billing is not switched on for this deployment yet.";
  }
  if (status === 409) {
    return "There is nothing to manage yet - choose a plan first.";
  }
  return "Could not open billing just now. Try again.";
}

/**
 * Only a redirect to Stripe's own hosted pages is followed. The URL comes from our API, but a
 * browser sent somewhere else by a compromised or misconfigured response would carry the person to
 * a page that looks like payment; refusing costs nothing when the answer is correct.
 */
export function isStripeUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return (
      parsed.protocol === "https:" &&
      (parsed.hostname === "checkout.stripe.com" || parsed.hostname === "billing.stripe.com")
    );
  } catch {
    return false;
  }
}
