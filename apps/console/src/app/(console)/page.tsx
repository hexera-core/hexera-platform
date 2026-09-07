/* eslint-disable @next/next/no-img-element */
import { redirect } from "next/navigation";
import Script from "next/script";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { ownerIdFromSession } from "@/lib/auth/session";

const legacyStylesheets = [
  "/static/css/tokens.css",
  "/static/css/shell.css",
  "/static/css/chat.css",
  "/static/css/timeline.css",
  "/static/css/result.css",
  "/static/css/viewer.css",
  "/static/css/a11y.css",
  "/static/css/theme.css",
];

export default async function ConsolePage() {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!ownerId) {
    redirect("/sign-in");
  }

  const publicApiBaseUrl = process.env.NEXT_PUBLIC_HEXERA_API_BASE_URL ?? "";

  return (
    <>
      {legacyStylesheets.map((href) => (
        <link href={href} key={href} rel="stylesheet" />
      ))}
      <Script id="hexera-console-session" strategy="beforeInteractive">
        {`
          globalThis.__HEXERA_API_WS_BASE_URL__ = ${JSON.stringify(publicApiBaseUrl)};
          try { localStorage.setItem("mg_uid", ${JSON.stringify(ownerId)}); } catch {}
        `}
      </Script>

      <div id="lb">
        <img alt="" id="lb-img" />
      </div>

      <div id="app">
        <header>
          <div className="brand">
            <img alt="" className="brand-mark" src="/static/assets/logo.png" />
            <span className="brand-name">HEXERA</span>
          </div>
          <div className="h-spacer" />
          <div
            aria-live="polite"
            className="chip chip-status"
            id="api-status"
            role="status"
          >
            <div className="dot" id="dot" />
            <span id="api-lbl">checking...</span>
          </div>
          <SignOutButton />
        </header>

        <div aria-live="polite" id="notice" role="status" />

        <div id="upload-bar">
          <input
            aria-label="Upload a geometry file"
            className="sr-only"
            data-accept-from-capabilities=""
            id="step-file-input"
            type="file"
          />
          <span id="file-label">No geometry file selected</span>
          <button id="upload-btn" type="button">
            Upload geometry
          </button>
        </div>

        <div id="stage" />

        <div id="input-bar">
          <div id="input-inner">
            <textarea
              aria-label="Message Hexera"
              disabled
              id="chat-input"
              placeholder="Upload a geometry file to begin..."
              rows={1}
            />
            <button disabled id="send-btn" type="button">
              Send
            </button>
          </div>
        </div>
      </div>

      <Script src="/static/js/main.js" strategy="afterInteractive" type="module" />
    </>
  );
}
