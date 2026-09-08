/* eslint-disable @next/next/no-img-element */
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { GoogleSignInButton } from "@/app/_components/auth-buttons";
import { LegacyStyles } from "@/app/_components/legacy-styles";

export default async function SignInPage() {
  const session = await auth();
  if (session) {
    redirect("/");
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
            <span>sign in required</span>
          </div>
        </header>

        <div id="stage">
          <div className="scroll">
            <div className="chat-col">
              <div id="empty">
                <p>Sign in to open the Hexera console.</p>
                <br />
                <GoogleSignInButton />
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
