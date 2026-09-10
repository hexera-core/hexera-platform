/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ForgotPasswordForm } from "@/app/_components/auth-forms";
import { LegacyStyles } from "@/app/_components/legacy-styles";

export default async function ForgotPasswordPage() {
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
            <span>reset your password</span>
          </div>
        </header>

        <div id="stage">
          <div className="scroll">
            <div className="chat-col">
              <div id="empty">
                <p>Enter the email on your Hexera console account.</p>
                <ForgotPasswordForm />
                <p>
                  <Link href="/sign-in">Back to sign in.</Link>
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
