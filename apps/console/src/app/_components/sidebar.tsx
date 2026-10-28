"use client";

/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { usePathname } from "next/navigation";

import { FilesPanel } from "@/app/_components/files-panel";
import { RecentRunsList } from "@/app/_components/recent-runs-list";
import type { RecentRun } from "@/app/_components/recent-runs";
import type { RunRead } from "@/app/_components/run-files";
import { CONSOLE_SECTIONS, currentSection, visibleSections } from "@/app/_components/sections";
import { PanelIcon, useSidebar } from "@/app/_components/shell";

type SidebarProps = {
  email: string;
  credits: string;
  /** The first read of the recent runs, done by the layout; null when the API could not say. */
  recent: RecentRun[] | null;
  /** The server-side CAD_REPAIR_POC switch, read by the layout; a Client Component cannot read
   *  the env itself. */
  repairPoc: boolean;
  /** Rendered, never imported: SignOutButton's inline "use server" action cannot be reached
   *  from a Client Component's import graph. The layout builds it and hands it over. */
  signOut: React.ReactNode;
  /** A first read of this run for the Files panel, when the caller has one; the panel reads it after. */
  files?: RunRead | null;
};

/** The slim sidebar: the brand and the hide switch, New run, the product sections, the recent
 *  runs, and the account tucked into one Settings fold at the foot. */
export function Sidebar({ credits, email, files = null, recent, repairPoc, signOut }: SidebarProps) {
  const pathname = usePathname();
  const current = currentSection(pathname);
  const { pane, setHidden, setPane } = useSidebar();
  // ON A RUN PAGE the slot has two jobs: the runs, or this run's files. Elsewhere it is the nav.
  const onRun = /^\/runs\/.+/.test(pathname);
  const showFiles = onRun && pane === "files";
  const sections = visibleSections(repairPoc);
  const product = sections.filter((section) => section.group === "product");
  const account = sections.filter((section) => section.group === "account");
  const inSettings = account.some((section) => section.href === current);

  return (
    <aside aria-label="Console" className="sidebar">
      <div className="sidebar__top">
        <Link className="sidebar__brand" href="/">
          <img alt="" src="/static/assets/logo.png" />
          <span>HEXERA</span>
        </Link>
        <button
          aria-label="Hide sidebar"
          className="sidebar__hide"
          onClick={() => setHidden(true)}
          title="Hide sidebar"
          type="button"
        >
          <PanelIcon />
        </button>
      </div>

      {onRun ? (
        <div aria-label="Side panel" className="sidebar__switch" role="group">
          <button aria-pressed={!showFiles} onClick={() => setPane("runs")} type="button">Runs</button>
          <button aria-pressed={showFiles} onClick={() => setPane("files")} type="button">Files</button>
        </div>
      ) : null}

      {showFiles ? <FilesPanel initial={files} /> : (<>
      <Link className="sidebar__new" href="/runs/new">
        <svg aria-hidden="true" className="sidebar__icon" fill="none" focusable="false"
          stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16">
          <path d="M8 3v10M3 8h10" />
        </svg>
        <span>New run</span>
      </Link>

      <nav aria-label="Console sections" className="sidebar__nav">
        {product.map((section) => (
          <Item current={current} key={section.href} section={section} />
        ))}
      </nav>

      <p className="sidebar__group label">Recent</p>
      <RecentRunsList initial={recent} />
      </>)}

      <div className="sidebar__foot">
        {/* the account pages, folded: they are visited rarely, and open by themselves when the
            user is on one of them */}
        <details className="sidebar__settings" open={inSettings || undefined}>
          <summary>
            <svg aria-hidden="true" className="sidebar__icon" fill="none" focusable="false"
              stroke="currentColor" strokeLinecap="round" strokeWidth="1.4" viewBox="0 0 16 16">
              <circle cx="8" cy="8" r="2" />
              <path d="M8 2.5v1.6M8 11.9v1.6M2.5 8h1.6M11.9 8h1.6M4.1 4.1l1.1 1.1M10.8 10.8l1.1 1.1M4.1 11.9l1.1-1.1M10.8 5.2l1.1-1.1" />
            </svg>
            <span>Settings</span>
          </summary>
          <nav aria-label="Account settings" className="sidebar__nav sidebar__nav--settings">
            {account.map((section) => (
              <Item current={current} key={section.href} section={section} />
            ))}
          </nav>
        </details>
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
        <Icon section={section} />
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
      <Icon section={section} />
      <span>{section.label}</span>
    </Link>
  );
}

function Icon({ section }: { section: (typeof CONSOLE_SECTIONS)[number] }) {
  return (
    <svg
      aria-hidden="true"
      className="sidebar__icon"
      fill="none"
      focusable="false"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.5"
      viewBox="0 0 24 24"
    >
      <path d={section.icon} />
    </svg>
  );
}
