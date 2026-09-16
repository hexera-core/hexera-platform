"use client";

import { usePathname } from "next/navigation";

/** The dashboard grid, and the one place that decides when it narrows to a rail.
 *
 * A CLIENT COMPONENT because a Server Component layout cannot read the pathname, and the rail is
 * a per-route state rather than a per-layout one. The same reason `Sidebar` is one.
 *
 * THE RAIL IS FOR THE WORKBENCH ONLY: `/runs/new` and `/runs/<id>` render a mesh that wants every
 * pixel of width, so the sidebar collapses to its icons there. `/runs` itself is an ordinary
 * table and keeps the full sidebar, which is why this tests for a segment BELOW /runs rather than
 * for the prefix.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const rail = /^\/runs\/.+/.test(pathname);
  return <div className={`shell${rail ? " shell--rail" : ""}`}>{children}</div>;
}
