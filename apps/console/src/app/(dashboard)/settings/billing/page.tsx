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
import { ManageBillingButton, PlanPicker } from "@/app/_components/billing-panel";
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
        <h1 className="page__title">Billing</h1>
      </div>

      {banner ? (
        <div className={banner.tone === "ok" ? "card" : "auth__error"} role="status">
          {banner.text}
        </div>
      ) : null}

      <div className="card" style={{ display: "grid", gap: ".5rem" }}>
        <p className="label">Current plan</p>
        <p style={{ fontSize: "1.8rem", fontFamily: "var(--mono)" }}>
          {subscription === null ? "unavailable" : planLabel(paying ? subscription.plan : "")}
        </p>
        <p className="page__sub">
          {subscription === null
            ? "Could not reach the API just now. Reload to try again."
            : subscriptionSummary(subscription)}
        </p>
        <p className="page__sub">
          Balance: {credits ? `${credits.balance.toLocaleString()} ${credits.unit}` : "unavailable"}
        </p>
        {subscription?.has_billing_account ? (
          <div>
            <ManageBillingButton />
          </div>
        ) : null}
      </div>

      <h2 className="label" style={{ marginTop: "1.5rem" }}>
        Plans
      </h2>
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
      <p className="page__sub">
        Prices are shown on the secure Stripe checkout page before you pay. Card details, invoices
        and cancellation are handled by Stripe under Manage billing.
      </p>
    </div>
  );
}
