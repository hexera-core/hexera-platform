import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ForgotPasswordForm } from "@/app/_components/auth-forms";
import { firebaseConfiguredOnServer } from "@/lib/firebase/server-config";

export default async function ForgotPasswordPage() {
  if (await auth()) {
    redirect("/");
  }

  const configured = firebaseConfiguredOnServer();

  return (
    <div className="auth__panel">
      <h1 className="auth__title">Reset your password</h1>
      <p className="auth__sub">
        Enter the address on your account and Identity Platform sends a reset link.
      </p>
      {configured ? (
        <>
          <ForgotPasswordForm />
          <div className="auth__alt">
            <Link href="/sign-in">Back to sign in</Link>
          </div>
        </>
      ) : (
        // Same rule as /sign-in and /sign-up: say so rather than offering a form that fails on
        // every submit.
        <div className="auth__error" role="status">
          Sign-in is not configured for this deployment.
        </div>
      )}
    </div>
  );
}
