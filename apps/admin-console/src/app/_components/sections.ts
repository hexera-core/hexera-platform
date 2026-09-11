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

export const ADMIN_SECTIONS: readonly AdminSection[] = [
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
    available: (process.env.OUTREACH_ENABLED ?? "").trim() === "1",
  },
] as const;
