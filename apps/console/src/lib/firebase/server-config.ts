// Server-side mirror of firebaseIsConfigured() in ./client. That module is "use client" (getAuth()
// assumes a browser), and a Server Component cannot call a plain function exported from a Client
// Component's module directly -- confirmed empirically against a live route: doing so throws
// "Attempted to call firebaseIsConfigured() from the server" at request time (a plain `next build`
// does not catch this because dynamic routes are never executed during its static-page pass).
// This three-env-var check is cheap enough to duplicate here rather than restructure that module,
// and every server component that needs it (sign-in, sign-up, forgot-password) imports this one
// copy so the duplication does not spread further than the one place it is unavoidable.
export function firebaseConfiguredOnServer(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_FIREBASE_API_KEY &&
      process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN &&
      process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  );
}
