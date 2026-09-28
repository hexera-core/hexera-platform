// The admin console's sections, in the order they are shown.
//
// `available` is the honest state of the PAGE, not of its data: it says whether following the link
// renders something. A section that is unavailable renders as text rather than a link, because
// linking to a page that cannot render is worse than saying why it is not there.
//
// Billing reads real figures now: the `organizations` and `credit_ledger` tables exist, PLANS is
// populated, and the Stripe integration fills both. Its panels still degrade individually, so the
// section stays available even where no payment provider is configured - the plans, balances and
// ledger are true without one, and only invoices need it. Activity, Customers and Outreach are
// launch operator surfaces: prod shows them, while dev keeps them out of the way unless a route is
// explicitly opened.
export type AdminSection = {
  href: string;
  label: string;
  available: boolean;
};

export function isLaunchAdminSurfaceEnabled(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return (env.ENV ?? env.APP_ENV ?? "").trim().toLowerCase() === "prod";
}

// Takes the environment rather than reading it, so both shapes - the deployment that runs
// outreach and the one that does not - are reachable from a test. A module-level constant read
// straight from process.env can only ever be asserted in whichever shape the test runner happens
// to be started in, which made the assertion below depend on the caller's shell.
export function buildAdminSections(
  env: Record<string, string | undefined> = process.env,
): readonly AdminSection[] {
  const base: AdminSection[] = [
    { href: "/", label: "Fleet", available: true },
    { href: "/costs", label: "Costs", available: true },
    { href: "/billing", label: "Billing", available: true },
  ];
  if (isLaunchAdminSurfaceEnabled(env)) {
    base.push(
      { href: "/activity", label: "Activity", available: true },
      { href: "/customers", label: "Customers", available: true },
    );
  }
  if ((env.OUTREACH_ENABLED ?? "").trim() === "1") {
    // Outreach is PROD-ONLY: one partner list, one mailbox. A dev copy would either duplicate the
    // real contacts or sit empty, and neither is worth a second Gmail connection. The deployment
    // says whether it runs here, so the flag decides rather than the hostname.
    base.push({ href: "/outreach", label: "Outreach", available: true });
  }
  return base;
}
