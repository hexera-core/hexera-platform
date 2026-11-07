"use client";

import { useEffect, useRef } from "react";

/** The safety net under NavLink: the browser's back and forward buttons, or any link that still
 *  navigates softly, can mount the workbench without a document load, and main.js would never
 *  start. The session script marks each fresh document; a workbench mounted without that mark
 *  reloads the page once, which starts it properly. The ref keeps React's double effect run in
 *  development from reading the mark twice. */
export function WorkbenchGuard() {
  const checked = useRef(false);
  useEffect(() => {
    if (checked.current) {
      return;
    }
    checked.current = true;
    const g = globalThis as { __HEXERA_FRESH_DOC__?: boolean };
    if (g.__HEXERA_FRESH_DOC__) {
      g.__HEXERA_FRESH_DOC__ = false;
      return;
    }
    window.location.reload();
  }, []);
  return null;
}
