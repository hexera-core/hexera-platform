/* eslint-disable @next/next/no-img-element */
import Script from "next/script";

export function Workbench({
  bootJobId,
  ownerId,
}: {
  bootJobId: string | null;
  ownerId: string;
}) {
  const publicApiBaseUrl = process.env.NEXT_PUBLIC_HEXERA_API_BASE_URL ?? "";

  return (
    <>
      <Script id="hexera-console-session" strategy="beforeInteractive">
        {`
          globalThis.__HEXERA_API_WS_BASE_URL__ = ${JSON.stringify(publicApiBaseUrl)};
          globalThis.__HEXERA_ROUTED__ = true;
          globalThis.__HEXERA_BOOT_JOB__ = ${JSON.stringify(bootJobId)};
          try { localStorage.setItem("mg_uid", ${JSON.stringify(ownerId)}); } catch {}
        `}
      </Script>

      <div id="lb">
        <img alt="" id="lb-img" />
      </div>

      <div id="app">
        <header>
          <button aria-pressed="false" className="chip" hidden id="wb-toggle" type="button">
            Hide conversation
          </button>
          <div className="h-spacer" />
          <div aria-live="polite" className="chip chip-status" id="api-status" role="status">
            <div className="dot" id="dot" />
            <span id="api-lbl">checking...</span>
          </div>
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
          <button className="btn btn--gold" id="upload-btn" type="button">
            Upload geometry
          </button>
        </div>

        <div id="stage" />
        <div aria-label="Delivered mesh" id="workbench" />

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
