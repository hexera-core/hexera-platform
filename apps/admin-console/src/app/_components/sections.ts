// The admin console's five sections, in the order they are shown.
//
// `available` is the honest state of the DATA, not a feature flag: Customers reads accounts and
// token metering, which do not exist yet, and Outreach is a single prod-only instance. A section
// that is unavailable renders as text rather than a link, because linking to a page that cannot
// render is worse than saying why it is not there.
export type AdminSection = {
  href: string;
  label: string;
  available: boolean;
};

export const ADMIN_SECTIONS: readonly AdminSection[] = [
  { href: "/", label: "Fleet", available: true },
  { href: "/costs", label: "Costs", available: true },
  { href: "/activity", label: "Activity", available: true },
  { href: "/customers", label: "Customers", available: false },
  { href: "/outreach", label: "Outreach", available: false },
] as const;
