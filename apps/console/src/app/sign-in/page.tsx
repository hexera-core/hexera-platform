/* eslint-disable @next/next/no-img-element */
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { EmailPasswordSignInForm } from "@/app/_components/auth-buttons";
import { LegacyStyles } from "@/app/_components/legacy-styles";

type SignInPageProps = {
  searchParams?: Promise<{
    error?: string | string[];
  }>;
};

export default async function SignInPage({ searchParams }: SignInPageProps) {
  const session = await auth();
  if (session) {
    redirect("/");
  }

  const params = await searchParams;
  const error = Array.isArray(params?.error) ? params.error[0] : params?.error;

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
                <EmailPasswordSignInForm hasError={error === "CredentialsSignin"} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
