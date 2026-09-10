/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignUpForm } from "@/app/_components/auth-forms";
import { LegacyStyles } from "@/app/_components/legacy-styles";
import { getHexeraApiClient } from "@/lib/hexera-api/server";
import { hexeraApiRoutes } from "@hexera/api-client";

type ClientConfig = {
  auth?: { signup_enabled?: boolean };
};

async function signupEnabled(): Promise<boolean> {
  try {
    const config = await getHexeraApiClient().request<ClientConfig>(hexeraApiRoutes.clientConfig);
    return config.auth?.signup_enabled !== false;
  } catch {
    // client-config is advisory only -- the real gate is on POST /auth/session -- so a failed
    // lookup should not block the console from offering the form it cannot otherwise verify.
    return true;
  }
}

export default async function SignUpPage() {
  const session = await auth();
  if (session) {
    redirect("/");
  }

  if (!(await signupEnabled())) {
    redirect("/sign-in?signup=closed");
  }

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
            <span>create an account</span>
          </div>
        </header>

        <div id="stage">
          <div className="scroll">
            <div className="chat-col">
              <div id="empty">
                <p>Create a Hexera console account.</p>
                <SignUpForm />
                <p>
                  <Link href="/sign-in">Already have an account? Sign in.</Link>
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
