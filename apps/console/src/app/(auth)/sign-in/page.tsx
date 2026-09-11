import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignInForm } from "@/app/_components/auth-forms";
import { firebaseConfiguredOnServer } from "@/lib/firebase/server-config";

type SignInPageProps = {
  searchParams?: Promise<{ signup?: string | string[] }>;
};

export default async function SignInPage({ searchParams }: SignInPageProps) {
  if (await auth()) {
    redirect("/");
  }
  const params = await searchParams;
  const signup = Array.isArray(params?.signup) ? params.signup[0] : params?.signup;

  return (
    <div className="auth__panel">
      <h1 className="auth__title">
        Sign in to <em>Hexera</em>
      </h1>
      {signup === "closed" ? (
        <div className="auth__error auth__note" role="status">
          This deployment is not accepting new accounts.
        </div>
      ) : null}
      {firebaseConfiguredOnServer() ? (
        <>
          <SignInForm />
          <div className="auth__alt">
            <Link href="/sign-up">Create an account</Link>
            <Link href="/forgot-password">Forgot your password?</Link>
          </div>
        </>
      ) : (
        // A console whose Identity Platform project was never set up must say so rather than
        // presenting a form that would fail on every submit.
        <div className="auth__error" role="status">
          Sign-in is not configured for this deployment.
        </div>
      )}
    </div>
  );
}
