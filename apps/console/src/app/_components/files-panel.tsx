"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { emptyWords, FILES_POLL_MS, fileRows, isLive, type RunRead, runIdFrom } from "./run-files";

/** The Files panel: what this run has delivered for the user to take away - the whole bundle, the
 *  mesh, the repaired CAD, the inspection report - listed as each one lands. It reads the run
 *  through the console's own API proxy, again every FILES_POLL_MS while the run is still working,
 *  and stops once it has ended. Nothing from the run's workspace is ever listed (run-files.ts).
 *
 *  A click asks for a fresh signed link (the same endpoint the workbench uses), so a link that
 *  expired while the panel sat open still downloads. Fails soft: a failed read keeps the rows. */
export function FilesPanel({ initial = null }: { initial?: RunRead | null } = {}) {
  const pathname = usePathname();
  // the workbench moves /runs/new to /runs/<id> itself once a run starts; read the address too
  const [href, setHref] = useState(pathname);
  const runId = runIdFrom(href) ?? runIdFrom(pathname);
  const [run, setRun] = useState<RunRead | null>(initial);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const read = async () => {
      const id = runIdFrom(window.location.pathname) ?? runIdFrom(pathname);
      setHref(window.location.pathname);
      if (!id) {
        timer = window.setTimeout(read, FILES_POLL_MS);
        return;
      }
      let next: RunRead | null = null;
      try {
        const r = await fetch(`/api/v1/simulation/${encodeURIComponent(id)}`, { cache: "no-store" });
        if (r.ok) {
          next = (await r.json()) as RunRead;
        }
      } catch {
        // the rows already shown stay; the next tick tries again
      }
      if (!alive) {
        return;
      }
      if (next) {
        setRun(next);
      }
      if (!next || isLive(next.status)) {
        timer = window.setTimeout(read, FILES_POLL_MS);
      }
    };
    void read();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [pathname]);

  const shown = run && runId && run.id === runId ? run : null;
  const rows = fileRows(shown?.artifacts);

  const download = async (artifactId: string) => {
    if (!runId) {
      return;
    }
    setBusy(artifactId);
    try {
      const r = await fetch(
        `/api/v1/simulation/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/download`,
        { cache: "no-store" },
      );
      const body = r.ok ? ((await r.json()) as { download_url?: string }) : null;
      if (body?.download_url) {
        window.location.assign(body.download_url);
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="files">
      {rows.length === 0 ? (
        <p className="files__empty">{emptyWords(runId, shown?.status)}</p>
      ) : (
        <ul className="files__list">
          {rows.map((row) => (
            <li key={row.id}>
              <button
                className={`files__row${row.main ? " files__row--main" : ""}`}
                disabled={busy === row.id}
                onClick={() => void download(row.id)}
                title={`Download ${row.label}`}
                type="button"
              >
                <svg aria-hidden="true" className="files__icon" fill="none" focusable="false" stroke="currentColor"
                  strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.4" viewBox="0 0 16 16">
                  {row.main ? <path d="M8 2.5v8M4.5 7L8 10.5 11.5 7M3 13.5h10" /> : <path d="M4 2h5l3 3v9H4z" />}
                </svg>
                <span className="files__name">{busy === row.id ? "Starting download…" : row.label}</span>
                <span className="files__size">{row.size}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {shown && isLive(shown.status) && rows.length > 0 ? (
        <p className="files__note">More files show up as Hexera writes them.</p>
      ) : null}
    </div>
  );
}
