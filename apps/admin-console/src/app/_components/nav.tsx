"use client";

import { usePathname } from "next/navigation";

import { ADMIN_SECTIONS } from "./sections";

// A client component only because the current section is the pathname, and a layout - which is
// where this renders - is not given one. The alternative is threading the path through every page.
export function AdminNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Admin sections" className="admin-nav">
      <ul>
        {ADMIN_SECTIONS.map((section) => (
          <li key={section.href}>
            {section.available ? (
              <a
                aria-current={section.href === pathname ? "page" : undefined}
                href={section.href}
              >
                {section.label}
              </a>
            ) : (
              <span className="admin-nav-unavailable" title="Not available yet">
                {section.label}
              </span>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
