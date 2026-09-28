"use client";

import { useState } from "react";

import {
  type ExtraCreditsOffer,
  type Plan,
  SALES_CONTACT,
  checkoutFailureMessage,
  creditCheckoutFailureMessage,
  extraCreditsLabel,
  extraCreditsUnavailable,
  isStripeUrl,
  planChangeFailureMessage,
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
  /** the tier this organisation pays for, or "" - a subscriber switches in place, never checks out */
  currentPlan: string;
  billingEnabled: boolean;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A SUBSCRIBER'S SWITCH is still hosted by Stripe. The API returns a deep link to the provider's
  // subscription-update flow; the tier shown here moves when the webhook lands.
  async function switchTo(plan: string) {
    setBusy(plan);
    setError(null);
    try {
      const message = await redirectTo("/api/v1/billing/plan", { plan }, planChangeFailureMessage);
      if (message) {
        setError(message);
        setBusy(null);
      }
    } catch {
      setError("Could not reach the API. Try again.");
      setBusy(null);
    }
  }

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
      <div className="billing-plan-grid">
        {plans.map((plan) => {
          const current = plan.name === currentPlan;
          return (
            <div className={`card billing-plan-card${current ? " card--accent" : ""}`} key={plan.name}>
              <p className="label">{planLabel(plan.name)}</p>
              <p style={{ fontSize: "1.4rem", fontFamily: "var(--mono)", color: "var(--text-bright)" }}>
                {plan.included_credits.toLocaleString()} credits / month
              </p>
              <p className="page__sub">{planTerms(plan)}</p>
              {current ? (
                <span className="status status--ok">Current plan</span>
              ) : currentPlan && plan.purchasable && billingEnabled ? (
                <button
                  className="btn"
                  disabled={busy !== null}
                  onClick={() => switchTo(plan.name)}
                  type="button"
                >
                  {busy === plan.name ? "Opening Stripe…" : `Switch to ${planLabel(plan.name)}`}
                </button>
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

export function ExtraCreditsPicker({ offer }: { offer: ExtraCreditsOffer | null | undefined }) {
  const [quantity, setQuantity] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!offer || extraCreditsUnavailable(offer)) {
    return null;
  }
  const activeOffer = offer;

  async function buy() {
    setBusy(true);
    setError(null);
    try {
      const message = await redirectTo(
        "/api/v1/billing/credits/checkout",
        { quantity },
        creditCheckoutFailureMessage,
      );
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
    <aside className="card card--accent" style={{ display: "grid", gap: ".75rem" }}>
      <div>
        <p className="label">One-off credits</p>
        <p style={{ fontSize: "1.55rem", fontFamily: "var(--mono)", color: "var(--text-bright)" }}>
          {extraCreditsLabel(activeOffer)}
        </p>
        <p className="page__sub">Buy extra capacity once. No subscription changes.</p>
      </div>
      <div className="billing-actions">
        <label className="label" htmlFor="extra-credit-packs">
          Packs
        </label>
        <input
          id="extra-credit-packs"
          min={1}
          max={100}
          onChange={(event) => {
            const next = Number(event.currentTarget.value);
            setQuantity(Number.isFinite(next) ? Math.min(Math.max(Math.trunc(next), 1), 100) : 1);
          }}
          style={{ width: "5rem" }}
          type="number"
          value={quantity}
        />
        <button className="btn btn--gold" disabled={busy} onClick={buy} type="button">
          {busy ? "Opening checkout…" : "Buy in Stripe"}
        </button>
      </div>
      {error ? (
        <div className="auth__error" role="status">
          {error}
        </div>
      ) : null}
    </aside>
  );
}
