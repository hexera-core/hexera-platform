"use client";

/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { usePathname } from "next/navigation";

import { CONSOLE_SECTIONS, currentSection } from "@/app/_components/sections";

type SidebarProps = {
  email: string;
  credits: string;
  /** Rendered, never imported: SignOutButton's inline "use server" action cannot be reached
   *  from a Client Component's import graph. The layout builds it and hands it over. */
  signOut: React.ReactNode;
};

export function Sidebar({ credits, email, signOut }: SidebarProps) {
  const current = currentSection(usePathname());
  const product = CONSOLE_SECTIONS.filter((section) => section.group === "product");
  const account = CONSOLE_SECTIONS.filter((section) => section.group === "account");

  return (
    <aside className="sidebar">
      <Link className="sidebar__brand" href="/">
        <img alt="" src="/static/assets/logo.png" />
        <span>HEXERA</span>
      </Link>

      <nav aria-label="Console sections" className="sidebar__nav">
        {product.map((section) => (
          <Item current={current} key={section.href} section={section} />
        ))}
        <p className="sidebar__group label">Account</p>
        {account.map((section) => (
          <Item current={current} key={section.href} section={section} />
        ))}
      </nav>

      <div className="sidebar__foot">
        <div className="sidebar__stat">
          <span className="label">Credits</span>
          <b>{credits}</b>
        </div>
        <div className="sidebar__account">
          <small title={email}>{email}</small>
          {signOut}
        </div>
      </div>
    </aside>
  );
}

function Item({
  current,
  section,
}: {
  current: string;
  section: (typeof CONSOLE_SECTIONS)[number];
}) {
  // An unavailable section renders as TEXT, never a link. Linking to a page that cannot render
  // is worse than saying why it is not there -- the admin console's own rule.
  if (!section.available) {
    return (
      <span className="sidebar__item sidebar__item--unavailable" title="Not available yet">
        <span>{section.label}</span>
      </span>
    );
  }
  const isCurrent = section.href === current;
  return (
    <Link
      aria-current={isCurrent ? "page" : undefined}
      className={`sidebar__item${isCurrent ? " sidebar__item--current" : ""}`}
      href={section.href}
    >
      <span>{section.label}</span>
    </Link>
  );
}
