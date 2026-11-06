// Whether the sidebar is shown, kept in a cookie so the server renders the page the way the user
// left it (no flash of a sidebar that then hides itself). A plain module: the layout, a Server
// Component, reads these values, so they cannot live in a "use client" file.

export const SIDEBAR_COOKIE = "hx_sidebar";

/** A year: the choice is a preference, not a session. */
const MAX_AGE_S = 365 * 24 * 3600;

/** Hidden only when the cookie says so. Anything else - no cookie, an old value - shows it. */
export function sidebarHidden(value: string | undefined | null): boolean {
  return value === "hidden";
}

/** The cookie that records the choice. */
export function sidebarCookie(hidden: boolean): string {
  return `${SIDEBAR_COOKIE}=${hidden ? "hidden" : "shown"}; path=/; max-age=${MAX_AGE_S}; samesite=lax`;
}
