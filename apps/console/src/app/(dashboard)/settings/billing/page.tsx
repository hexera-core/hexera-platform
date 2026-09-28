import { redirect } from "next/navigation";

import { auth } from "@/auth";
import {
  type Catalogue,
  type Subscription,
  checkoutBanner,
  isPaying,
  planLabel,
  subscriptionSummary,
} from "@/app/_components/billing";
import { ExtraCreditsPicker, ManageBillingButton, PlanPicker } from "@/app/_components/billing-panel";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

// THIS PATH IS FIXED BY THE STRIPE ADAPTER: checkout's success and cancel URLs and the portal's
// return URL all point at /settings/billing (adapters/stripe_billing). Moving the page means moving
// those three with it, or a customer who has just paid lands on a 404.
export default async function BillingPage({
  searchParams,
}: {
  searchParams?: Promise<{ checkout?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const banner = checkoutBanner(
    Array.isArray(params?.checkout) ? params.checkout[0] : params?.checkout,
  );

  // Three independent soft reads, so one failing panel does not blank the others.
  const [subscription, catalogue, credits] = await Promise.all([
    consoleFetch<Subscription>("billing", ownerId),
    consoleFetch<Catalogue>("billing/plans", ownerId),
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
  ]);
  const paying = isPaying(subscription);

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Commercial control</p>
          <h1 className="page__title">Plans and credits</h1>
          <p className="page__intro">
            Manage the subscription that grants monthly capacity and add one-off credits when a
            study needs more room.
          </p>
        </div>
        {subscription?.has_billing_account ? <ManageBillingButton /> : null}
      </div>

      {banner ? (
        <div className={banner.tone === "ok" ? "card" : "auth__error"} role="status">
          {banner.text}
        </div>
      ) : null}

      <div className="summary-grid">
        <div className="metric-card">
          <p className="label">Current plan</p>
          <b>{subscription === null ? "Unavailable" : planLabel(paying ? subscription.plan : "")}</b>
          <span>{subscription === null ? "API unavailable" : subscriptionSummary(subscription)}</span>
        </div>
        <div className="metric-card">
          <p className="label">Credit balance</p>
          <b>{credits ? credits.balance.toLocaleString() : "--"}</b>
          <span>{credits ? credits.unit : "unavailable"} available</span>
        </div>
        <div className="metric-card">
          <p className="label">Payment system</p>
          <b>Stripe</b>
          <span>Checkout, payment methods, invoices and cancellation are provider-hosted.</span>
        </div>
      </div>

      <div className="billing-layout">
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">Plans</p>
              <p className="page__sub">Monthly capacity, limits and hosted Stripe plan changes.</p>
            </div>
          </div>
          <div className="panel__body">
            {catalogue === null ? (
              <p className="empty">Could not load plans just now. Reload to try again.</p>
            ) : !catalogue.billing_enabled ? (
              <p className="empty">Plans are not on sale in this deployment yet.</p>
            ) : (
              <PlanPicker
                billingEnabled={catalogue.billing_enabled}
                currentPlan={paying && subscription ? subscription.plan : ""}
                plans={catalogue.plans}
              />
            )}
          </div>
        </section>

        {catalogue?.extra_credits ? <ExtraCreditsPicker offer={catalogue.extra_credits} /> : null}
      </div>
    </div>
  );
}
