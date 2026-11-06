"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { RECENT_LIMIT, RECENT_POLL_MS, type RecentRun, recentRow } from "./recent-runs";

/** The sidebar's recent runs. The layout hands over the first read (so the list is there on the
 *  first paint); after that it is read again through the console's own API proxy on every
 *  navigation and every RECENT_POLL_MS while the tab is visible. A run the workbench starts
 *  happens in main.js, outside React, so a poll is the only way the list hears of it.
 *
 *  Fails soft: a failed read keeps the rows it has. */
export function RecentRunsList({ initial }: { initial: RecentRun[] | null }) {
  const pathname = usePathname();
  const [runs, setRuns] = useState<RecentRun[] | null>(initial);
  // no clock until the first read lands: the server and the browser would not agree on "12 min"
  // versus "13 min", and a hydration mismatch is worse than an age that appears a moment later
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    const read = async () => {
      if (document.visibilityState === "hidden") {
        return;
      }
      try {
        const r = await fetch(`/api/v1/simulation?limit=${RECENT_LIMIT}`, { cache: "no-store" });
        if (!r.ok) {
          return;
        }
        const body = (await r.json()) as { items?: RecentRun[] };
        if (alive && Array.isArray(body.items)) {
          setRuns(body.items);
          setNow(Date.now());
        }
      } catch {
        // the rows already shown stay; the next tick tries again
      }
    };
    void read();
    const timer = window.setInterval(read, RECENT_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [pathname]);

  if (runs === null) {
    return <p className="sidebar__empty">Couldn&apos;t load your runs.</p>;
  }
  if (runs.length === 0) {
    return <p className="sidebar__empty">No runs yet.</p>;
  }
  return (
    <ul className="sidebar__runs">
      {runs.map((run) => {
        const row = recentRow(run, now ?? Number.NaN);
        const current = pathname === row.href;
        return (
          <li key={run.id}>
            <Link
              aria-current={current ? "page" : undefined}
              className={`sidebar__run${current ? " sidebar__run--current" : ""}`}
              href={row.href}
              title={`${row.title} - ${row.words}`}
            >
              <span aria-hidden="true" className={`sidebar__dot${row.dot ? ` sidebar__dot--${row.dot}` : ""}`} />
              <span className="sidebar__run-title">{row.title}</span>
              <span className="sr-only">, {row.words}</span>
              <span className="sidebar__run-age">{row.age}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
