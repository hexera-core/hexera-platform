// Responsibility: Say whether a console link leads to the workbench (a run page or New run).
// Boundaries: pure. It reads no router and renders nothing.

/** The workbench (main.js and its markup) only starts on a full document load: main.js is an ES
 *  module, which a browser runs once per document, and its session facts come from an inline
 *  script that React never runs on a client-side navigation. A soft navigation to a run page
 *  therefore showed an empty, "checking..." workbench until the page was reloaded. Links into
 *  the workbench are plain document loads for that reason. */
export function isWorkbenchHref(href: string): boolean {
  const path = href.split(/[?#]/, 1)[0] ?? "";
  return path === "/runs/new" || /^\/runs\/[^/]+$/.test(path);
}
