// The admin console's sections, in the order they are shown.
//
// `available` is the honest state of the PAGE, not of its data: it says whether following the link
// renders something. A section that is unavailable renders as text rather than a link, because
// linking to a page that cannot render is worse than saying why it is not there.
//
// Billing is available even though it has no data. That is not an inconsistency with the rule
// above - the page renders, and every panel on it states which table it is waiting for. Customers
// stays unavailable because there is no page at all, and Outreach because it is a single prod-only
// instance that this deployment may not be.
export type AdminSection = {
  href: string;
  label: string;
  available: boolean;
};

// Takes the environment rather than reading it, so both shapes - the deployment that runs
// outreach and the one that does not - are reachable from a test. A module-level constant read
// straight from process.env can only ever be asserted in whichever shape the test runner happens
// to be started in, which made the assertion below depend on the caller's shell.
export function buildAdminSections(
  env: Record<string, string | undefined> = process.env,
): readonly AdminSection[] {
  return [
    { href: "/", label: "Fleet", available: true },
    { href: "/costs", label: "Costs", available: true },
    { href: "/billing", label: "Billing", available: true },
    { href: "/activity", label: "Activity", available: false },
    { href: "/customers", label: "Customers", available: false },
    // Outreach is PROD-ONLY: one partner list, one mailbox. A dev copy would either duplicate the
    // real contacts or sit empty, and neither is worth a second Gmail connection. The deployment
    // says whether it runs here, so the flag decides rather than the hostname.
    {
      href: "/outreach",
      label: "Outreach",
      available: (env.OUTREACH_ENABLED ?? "").trim() === "1",
    },
  ];
}

export const ADMIN_SECTIONS: readonly AdminSection[] = buildAdminSections();
