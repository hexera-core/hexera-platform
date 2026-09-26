"use client";

import { useState } from "react";

import {
  type Plan,
  SALES_CONTACT,
  checkoutFailureMessage,
  isStripeUrl,
  planLabel,
  planTerms,
  portalFailureMessage,
} from "@/app/_components/billing";

// THE TWO ACTIONS THAT LEAVE THE CONSOLE. Both ask the API for a Stripe-hosted URL and send the
// browser there; nothing about a card or an invoice is ever handled here. What a plan BECOMES is
// decided by the webhook, not by this page coming back - see billing.ts checkoutBanner.

async function redirectTo(
  path: string,
  body: unknown,
  failure: (status: number, detail: string) => string,
) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    const detail = await response
      .json()
      .then((payload: { detail?: unknown }) => (typeof payload.detail === "string" ? payload.detail : ""))
      .catch(() => "");
    return failure(response.status, detail);
  }
  const { url } = (await response.json()) as { url?: string };
  if (!url || !isStripeUrl(url)) {
    return "Billing answered with an unexpected address, so we did not follow it. Try again.";
  }
  window.location.assign(url);
  return null;
}

export function ManageBillingButton() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function open() {
    setBusy(true);
    setError(null);
    try {
      const message = await redirectTo("/api/v1/billing/portal", {}, portalFailureMessage);
      if (message) {
        setError(message);
        setBusy(false);
      }
    } catch {
      setError("Could not reach the API. Try again.");
      setBusy(false);
    }
  }

  return (
    <>
      <button className="btn" disabled={busy} onClick={open} type="button">
        Manage billing
      </button>
      {error ? (
        <div className="auth__error" role="status">
          {error}
        </div>
      ) : null}
    </>
  );
}

export function PlanPicker({
  plans,
  currentPlan,
  billingEnabled,
}: {
  plans: Plan[];
  /** the tier this organisation pays for, or "" - a subscriber changes tier in the portal */
  currentPlan: string;
  billingEnabled: boolean;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function choose(plan: string) {
    setBusy(plan);
    setError(null);
    try {
      const message = await redirectTo("/api/v1/billing/checkout", { plan }, checkoutFailureMessage);
      if (message) {
        setError(message);
        setBusy(null);
      }
    } catch {
      setError("Could not reach the API. Try again.");
      setBusy(null);
    }
  }

  return (
    <>
      {error ? (
        <div className="auth__error" role="status">
          {error}
        </div>
      ) : null}
      <div style={{ display: "grid", gap: "1rem", gridTemplateColumns: "repeat(auto-fit, minmax(14rem, 1fr))" }}>
        {plans.map((plan) => {
          const current = plan.name === currentPlan;
          return (
            <div className="card" key={plan.name} style={{ display: "grid", gap: ".5rem" }}>
              <p className="label">{planLabel(plan.name)}</p>
              <p style={{ fontSize: "1.4rem", fontFamily: "var(--mono)" }}>
                {plan.included_credits.toLocaleString()} credits / month
              </p>
              <p className="page__sub">{planTerms(plan)}</p>
              {current ? (
                <span className="status status--ok">Current plan</span>
              ) : currentPlan && plan.purchasable ? (
                // A SUBSCRIBER CHANGES TIER IN THE PORTAL. A checkout here would open a second
                // subscription beside the first - two flat fees - and the API refuses one.
                <span className="page__sub">Switch under Manage billing</span>
              ) : plan.purchasable && billingEnabled ? (
                <button
                  className="btn btn--gold"
                  disabled={busy !== null}
                  onClick={() => choose(plan.name)}
                  type="button"
                >
                  {busy === plan.name ? "Opening checkout…" : `Choose ${planLabel(plan.name)}`}
                </button>
              ) : (
                // NOT PURCHASABLE is two situations: an invoiced tier (enterprise), and a tier
                // whose price is not configured in this deployment. Both are a conversation, not
                // a button that fails inside Stripe's page.
                <a className="btn" href={SALES_CONTACT}>
                  Contact us
                </a>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
