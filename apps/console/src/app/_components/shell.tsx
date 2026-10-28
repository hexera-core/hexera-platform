"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";

import { sidebarCookie } from "./sidebar-state";

type SidebarControl = { hidden: boolean; setHidden: (hidden: boolean) => void };

const SidebarContext = createContext<SidebarControl>({ hidden: false, setHidden: () => {} });

/** The sidebar's show/hide switch, for the sidebar's own hide button. */
export function useSidebar(): SidebarControl {
  return useContext(SidebarContext);
}

/** The dashboard grid, and the one place that decides whether the sidebar is shown.
 *
 * A CLIENT COMPONENT because the choice changes on a click. It starts from the cookie the layout
 * read (`initialHidden`), so the server already renders the page the way the user left it, and
 * every click writes the cookie back.
 *
 * THE RAIL IS GONE. The run routes used to narrow the sidebar to a column of icons to give the
 * mesh its width. Now the sidebar is slim on every route, and the user hides it whenever they
 * want the whole width - one button, the same on every page. On narrow screens the sidebar is a
 * bar across the top and the choice does not apply (dashboard.css).
 */
export function Shell({
  children,
  initialHidden,
}: {
  children: React.ReactNode;
  initialHidden: boolean;
}) {
  const [hidden, setHiddenState] = useState(initialHidden);
  const setHidden = useCallback((next: boolean) => {
    setHiddenState(next);
    document.cookie = sidebarCookie(next);
  }, []);
  const control = useMemo(() => ({ hidden, setHidden }), [hidden, setHidden]);
  return (
    <SidebarContext.Provider value={control}>
      <div className={`shell${hidden ? " shell--hidden" : ""}`}>
        {children}
        {hidden ? (
          <button
            aria-label="Show sidebar"
            className="shell__show"
            onClick={() => setHidden(false)}
            title="Show sidebar"
            type="button"
          >
            <PanelIcon />
          </button>
        ) : null}
      </div>
    </SidebarContext.Provider>
  );
}

export function PanelIcon() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      focusable="false"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.4"
      viewBox="0 0 16 16"
    >
      <rect height="10" rx="1.5" width="12" x="2" y="3" />
      <path d="M6 3v10" />
    </svg>
  );
}
