/* eslint-disable @next/next/no-img-element */
import Script from "next/script";

import { sessionScript } from "./session-script";

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
      {/* THE SESSION FACTS main.js BOOTS FROM, as a plain inline <script> - not next/script.
          `beforeInteractive` is honoured only from the root layout. Anywhere else Next renders it
          as a push onto `self.__next_s`, a queue its runtime drains ONCE when its own async chunk
          runs; from a page in the streamed body the push lands after the drain and never executes,
          so every global here stayed undefined. Everything else fell back and kept working - the
          run went to the URL as ?job=, the deep link read it back - except the WebSocket origin:
          unset means "this host", the console cannot serve a socket, and the browser dialled it
          every 30 seconds for the whole run (503 in 2 ms, each time). The timeline stayed empty,
          live and on replay, and a failed run showed no reason. A plain script runs where it is
          parsed, which is before main.js - loaded by Next after hydration. */}
      <script
        dangerouslySetInnerHTML={{
          __html: sessionScript({ bootJobId, ownerId, publicApiBaseUrl }),
        }}
        id="hexera-console-session"
      />

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
