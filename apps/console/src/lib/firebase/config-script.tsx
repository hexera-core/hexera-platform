import Script from "next/script";

import { firebaseConfiguredOnServer } from "@/lib/firebase/server-config";

/** The global the client module reads its configuration out of. */
export const FIREBASE_CONFIG_GLOBAL = "__HEXERA_FIREBASE_CONFIG__";

// WHY THIS EXISTS AT ALL, because `process.env.NEXT_PUBLIC_*` looks like it should be enough and
// is not. Next INLINES a NEXT_PUBLIC_ value into the client bundle when that bundle is COMPILED.
// This console's image is built in CI, where the Identity Platform values are not set, so the
// browser bundle was compiled with `apiKey: undefined` no matter what Cloud Run supplies at
// runtime - and the server-side check still passed, so the page rendered a sign-up form whose
// every submit threw. `.env.example` already records the mirror image of this for
// NEXT_PUBLIC_HEXERA_API_BASE_URL: it survives only because a SERVER component reads it and
// injects it into the page, which is exactly what this does.
//
// Build arguments would fix the inlining and break something worse: one validated console digest
// is promoted across environments, so a value frozen at build time would ship dev's project to
// prod. Injecting at render keeps the image environment-agnostic, which is what promotion by
// digest requires.
export function FirebaseConfigScript() {
  if (!firebaseConfiguredOnServer()) {
    // Nothing to inject. The pages already render "not configured" in this case, and emitting an
    // empty object would make a misconfiguration look like a Firebase fault instead.
    return null;
  }
  const config = {
    apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
    authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  };
  return (
    /* The rule's message is the Pages Router's: it says `pages/_document.js`. This version's own
       docs say the opposite for the App Router - node_modules/next/dist/docs/01-app/
       03-api-reference/02-components/script.md: "Scripts with the `beforeInteractive` strategy
       must be placed inside a root layout, such as `app/layout.tsx`". That is where this renders
       from, so the rule is stale here rather than the placement being wrong. The strategy is
       required, not cosmetic: the config must exist before hydration or the SDK initialises with
       an undefined key, which is the bug this file exists to fix. */
    // eslint-disable-next-line @next/next/no-before-interactive-script-outside-document
    <Script id="hexera-firebase-config" strategy="beforeInteractive">
      {`globalThis.${FIREBASE_CONFIG_GLOBAL} = ${JSON.stringify(config)};`}
    </Script>
  );
}
