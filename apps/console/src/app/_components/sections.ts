// The product console's sections, in the order the sidebar shows them.
//
// `available` is the honest state of the DATA, not a feature flag, and it is kept even though
// every section is available today -- the same field the admin console's ADMIN_SECTIONS carries,
// for the same reason: a section that cannot render must say why rather than link to a page that
// cannot. The next cycle that adds a section before its endpoint needs this field to already be
// here.
export type ConsoleSection = {
  href: string;
  label: string;
  group: "product" | "account";
  available: boolean;
  /** The mark beside the label. On a viewport under 860px the sidebar becomes a bar and the
   *  icon is what the eye finds first. SVG path data for a 24x24 viewBox, stroked in
   *  currentColor. */
  icon: string;
};

export const CONSOLE_SECTIONS: readonly ConsoleSection[] = [
  // One product surface: a run is the unit of work, and /runs/new and /runs/<id> are the same
  // workbench at different moments. The old Overview, Conversations and Usage lists each showed a
  // slice of the same jobs; they folded into /runs (the list) and /settings/billing (the money).
  { href: "/runs", label: "Runs", group: "product", available: true,
    icon: "M4 6h16M4 12h16M4 18h10" },
  { href: "/settings/billing", label: "Billing", group: "account", available: true,
    icon: "M3 7h18v10H3zM3 11h18M7 15h3" },
  { href: "/settings/account", label: "Account", group: "account", available: true,
    icon: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0" },
  { href: "/settings/api-keys", label: "API keys", group: "account", available: true,
    icon: "M14 8a4 4 0 1 1-3.9 5H8l-2 2-2-2 2-2h2.1A4 4 0 0 1 14 8z" },
  { href: "/settings/organization", label: "Organisation", group: "account", available: true,
    icon: "M4 21V7l7-4 7 4v14M9 21v-5h6v5" },
] as const;

/** Which section's href owns this path, or "" if none does.
 *
 * Longest match wins, so "/settings/api-keys" beats a hypothetical "/settings". No section owns
 * "/" - it redirects to /runs - so the root correctly falls through to "".
 */
export function currentSection(pathname: string): string {
  const matches = CONSOLE_SECTIONS.filter(
    (section) =>
      pathname === section.href || pathname.startsWith(`${section.href}/`),
  );
  return matches.reduce((longest, section) =>
    section.href.length > longest.href.length ? section : longest,
    { href: "", label: "", group: "product", available: true, icon: "" } as ConsoleSection,
  ).href;
}
