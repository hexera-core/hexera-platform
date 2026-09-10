/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignInForm } from "@/app/_components/auth-forms";
import { LegacyStyles } from "@/app/_components/legacy-styles";

type SignInPageProps = {
  searchParams?: Promise<{
    signup?: string | string[];
  }>;
};

// Mirrors firebaseIsConfigured() in @/lib/firebase/client, deliberately not imported: that module
// is "use client" (getAuth() assumes a browser), and Next.js refuses to invoke a plain function
// exported from a Client Component's module from a Server Component -- confirmed against this
// exact route, which throws "Attempted to call firebaseIsConfigured() from the server" at
// request time (a build alone does not catch it, because this route is dynamic and never
// executes during the build's static-page pass). The check itself is three env reads, so
// duplicating it here is simpler than restructuring the client module to split it out.
function firebaseConfiguredOnServer(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_FIREBASE_API_KEY &&
      process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN &&
      process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  );
}

export default async function SignInPage({ searchParams }: SignInPageProps) {
  const session = await auth();
  if (session) {
    redirect("/");
  }

  const params = await searchParams;
  const signup = Array.isArray(params?.signup) ? params.signup[0] : params?.signup;

  return (
    <>
      <LegacyStyles />

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
            role="status"
          >
            <div className="dot" />
            <span>sign in required</span>
          </div>
        </header>

        <div id="stage">
          <div className="scroll">
            <div className="chat-col">
              <div id="empty">
                <p>Sign in to open the Hexera console.</p>
                {signup === "closed" ? (
                  <p role="status">This deployment is not accepting new accounts.</p>
                ) : null}
                {firebaseConfiguredOnServer() ? (
                  <>
                    <SignInForm />
                    <p>
                      <Link href="/sign-up">Create an account.</Link>{" "}
                      <Link href="/forgot-password">Forgot your password?</Link>
                    </p>
                  </>
                ) : (
                  // A console whose Identity Platform project was never set up must say so
                  // rather than presenting a form that would fail on every submit.
                  <p role="status">Sign-in is not configured for this deployment.</p>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
