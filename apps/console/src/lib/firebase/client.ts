"use client";

import { getApp, getApps, initializeApp, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";

// PUBLIC BY DESIGN. The Firebase web API key identifies the project; it authorises nothing on
// its own, which is why it ships as plain environment and not as a Secret Manager reference.
// The gate that matters is CONSOLE_SIGNUP_ENABLED on the product API.
type FirebaseConfig = {
  apiKey?: string;
  authDomain?: string;
  projectId?: string;
};

// READ AT CALL TIME FROM A RUNTIME-INJECTED GLOBAL, not from process.env at module scope. Next
// inlines NEXT_PUBLIC_* into the client bundle when it COMPILES it, and this console's image is
// built in CI without these values - so reading process.env here compiled to `undefined` and
// every SDK call threw, while the server-side check still passed and rendered the form. See
// lib/firebase/config-script.tsx, which injects this global from a server component.
//
// process.env remains the fallback so `next dev` works locally, where the value IS present at
// build time and no injection has happened yet.
function config(): FirebaseConfig {
  const injected = (globalThis as Record<string, unknown>)[
    "__HEXERA_FIREBASE_CONFIG__"
  ] as FirebaseConfig | undefined;
  return (
    injected ?? {
      apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
      authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
      projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    }
  );
}

function app(): FirebaseApp {
  // Next re-executes modules across client navigations; initializeApp twice throws.
  return getApps().length ? getApp() : initializeApp(config());
}

export function firebaseAuth(): Auth {
  return getAuth(app());
}

export function firebaseIsConfigured(): boolean {
  const c = config();
  return Boolean(c.apiKey && c.authDomain && c.projectId);
}
