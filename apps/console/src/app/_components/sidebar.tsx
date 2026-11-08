"use client";

/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { usePathname } from "next/navigation";

import { NavLink } from "@/app/_components/nav-link";
import { FilesPanel } from "@/app/_components/files-panel";
import { RecentRunsList } from "@/app/_components/recent-runs-list";
import type { RecentRun } from "@/app/_components/recent-runs";
import type { RunRead } from "@/app/_components/run-files";
import { formatCredits } from "@/app/_components/ledger";
import { CONSOLE_SECTIONS, currentSection } from "@/app/_components/sections";
import { PanelIcon, useSidebar } from "@/app/_components/shell";

type SidebarProps = {
  email: string;
  /** The credit balance, or null when the API could not say. */
  credits: number | null;
  /** The first read of the recent runs, done by the layout; null when the API could not say. */
  recent: RecentRun[] | null;
  /** Rendered, never imported: SignOutButton's inline "use server" action cannot be reached
   *  from a Client Component's import graph. The layout builds it and hands it over. */
  signOut: React.ReactNode;
  /** A first read of this run for the Files panel, when the caller has one; the panel reads it after. */
  files?: RunRead | null;
};

/** One stroke weight and one box for every mark in the sidebar, so the column lines up. */
const NEW_RUN_ICON = "M12 5v14M5 12h14";
const SETTINGS_ICON = "M4 7h9m4 0h3M4 17h3m4 0h9M15 5v4M9 15v4";

/** The slim sidebar: the brand and the hide switch, New run, the product sections, the recent
 *  runs, and the account tucked into one Settings fold at the foot. */
export function Sidebar({ credits, email, files = null, recent, signOut }: SidebarProps) {
  const pathname = usePathname();
  // a new run is New run's page, not a run in the list: no section is marked while it is open
  const onNewRun = pathname === "/runs/new";
  const current = onNewRun ? "" : currentSection(pathname);
  const { pane, setHidden, setPane } = useSidebar();
  // ON A RUN PAGE the slot has two jobs: the runs, or this run's files. Elsewhere it is the nav.
  const onRun = /^\/runs\/(?!new$).+/.test(pathname);
  const showFiles = onRun && pane === "files";
  const product = CONSOLE_SECTIONS.filter((section) => section.group === "product");
  const account = CONSOLE_SECTIONS.filter((section) => section.group === "account");
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
      <NavLink aria-current={onNewRun ? "page" : undefined}
        className={`sidebar__new${onNewRun ? " sidebar__new--current" : ""}`} href="/runs/new">
        <Mark d={NEW_RUN_ICON} />
        <span>New run</span>
      </NavLink>

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
            <Mark d={SETTINGS_ICON} />
            <span>Settings</span>
          </summary>
          <nav aria-label="Account settings" className="sidebar__nav sidebar__nav--settings">
            {account.map((section) => (
              <Item current={current} key={section.href} section={section} />
            ))}
          </nav>
        </details>
        <Credits balance={credits} />
        <div className="sidebar__account">
          <span aria-hidden="true" className="sidebar__avatar">{(email.trim()[0] ?? "?").toUpperCase()}</span>
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

/** The balance, said plainly. A balance at or below zero is shown as it is - in the warning
 *  tone, with a true minus sign - and beside it the one thing to do about it. */
function Credits({ balance }: { balance: number | null }) {
  const low = balance !== null && balance <= 0;
  return (
    <div className={`sidebar__credits${low ? " sidebar__credits--low" : ""}`}>
      <Link className="sidebar__credits-main" href="/usage" title="See usage">
        <span>Credits</span>
        <b>{balance === null ? "—" : formatCredits(balance)}</b>
      </Link>
      {low ? (
        <Link className="sidebar__topup" href="/settings/billing">Top up</Link>
      ) : null}
    </div>
  );
}

function Icon({ section }: { section: (typeof CONSOLE_SECTIONS)[number] }) {
  return <Mark d={section.icon} />;
}

function Mark({ d }: { d: string }) {
  return (
    <svg
      aria-hidden="true"
      className="sidebar__icon"
      fill="none"
      focusable="false"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.6"
      viewBox="0 0 24 24"
    >
      <path d={d} />
    </svg>
  );
}
