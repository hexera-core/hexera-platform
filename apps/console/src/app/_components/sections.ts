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
};

export const CONSOLE_SECTIONS: readonly ConsoleSection[] = [
  { href: "/", label: "Overview", group: "product", available: true },
  { href: "/runs", label: "Runs", group: "product", available: true },
  { href: "/conversations", label: "Conversations", group: "product", available: true },
  { href: "/usage", label: "Usage", group: "product", available: true },
  { href: "/settings/account", label: "Account", group: "account", available: true },
  { href: "/settings/api-keys", label: "API keys", group: "account", available: true },
  { href: "/settings/organization", label: "Organisation", group: "account", available: true },
] as const;

/** Which section's href owns this path, or "" if none does.
 *
 * Longest match wins, so "/settings/api-keys" beats a hypothetical "/settings". The root is
 * matched exactly rather than by prefix: "/" is a prefix of every path, and a naive startsWith
 * would leave Overview marked current on every page in the console.
 */
export function currentSection(pathname: string): string {
  if (pathname === "/") {
    return "/";
  }
  const matches = CONSOLE_SECTIONS.filter(
    (section) =>
      section.href !== "/" &&
      (pathname === section.href || pathname.startsWith(`${section.href}/`)),
  );
  return matches.reduce((longest, section) =>
    section.href.length > longest.href.length ? section : longest,
    { href: "", label: "", group: "product", available: true } as ConsoleSection,
  ).href;
}
