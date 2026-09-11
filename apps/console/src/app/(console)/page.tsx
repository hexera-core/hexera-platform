/* eslint-disable @next/next/no-img-element */
import { redirect } from "next/navigation";
import Script from "next/script";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { LegacyStyles } from "@/app/_components/legacy-styles";
import { ownerIdFromSession } from "@/lib/auth/session";
import { proxyProductApiRequest } from "@/lib/product-api/proxy";

type CreditBalance = {
  balance: number;
  unit: string;
};

async function creditBalance(ownerId: string): Promise<CreditBalance | null> {
  try {
    // Goes through the same authenticated proxy path the browser's own /api/v1 calls use, so
    // this never needs its own copy of the identity-header signing logic in proxy.ts.
    //
    // Both the try/catch and the race against a timeout matter here, and neither alone is
    // enough: fetch() REJECTS (rather than resolving with a non-ok Response) on a connection
    // failure -- refused, DNS failure -- and an uncaught rejection here previously propagated
    // out of the whole page component, taking down the entire authenticated console instead of
    // just this chip. A slow-but-not-failing API is just as damaging without a bound on time, so
    // the timeout keeps a hang from stalling the page's whole server render the same way the
    // catch keeps a crash from doing the same.
    const response = await Promise.race([
      proxyProductApiRequest(
        new Request("http://console.internal/api/v1/credits"),
        ["credits"],
        ownerId,
      ),
      rejectOnAbort(AbortSignal.timeout(3_000)),
    ]);
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as CreditBalance;
  } catch {
    // Fails open: a down, unreachable or slow product API degrades the chip, not the page.
    return null;
  }
}

function rejectOnAbort(signal: AbortSignal): Promise<never> {
  return new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason ?? new Error("timed out")));
  });
}

export default async function ConsolePage() {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!session || !ownerId) {
    redirect("/sign-in");
  }

  const credits = await creditBalance(ownerId);
  const publicApiBaseUrl = process.env.NEXT_PUBLIC_HEXERA_API_BASE_URL ?? "";

  return (
    <>
      <LegacyStyles />
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
          <button
            aria-pressed="false"
            className="chip"
            hidden
            id="wb-toggle"
            type="button"
          >
            Hide conversation
          </button>
          <div
            aria-live="polite"
            className="chip chip-status"
            id="api-status"
            role="status"
          >
            <div className="dot" id="dot" />
            <span id="api-lbl">checking...</span>
          </div>
          <div aria-live="polite" className="chip" id="credit-balance" role="status">
            <span>{credits ? `${credits.balance} ${credits.unit}` : "-- credits"}</span>
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

        {/* THE WORKBENCH: empty until a mesh is delivered, then the mesh fills it and the
            conversation above becomes the drawer beside it (see css/workbench.css). */}
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
