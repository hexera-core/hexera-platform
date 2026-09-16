import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignUpForm } from "@/app/_components/auth-forms";
import { getHexeraApiClient } from "@/lib/hexera-api/server";
import { firebaseConfiguredOnServer } from "@/lib/firebase/server-config";
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
  if (await auth()) {
    redirect("/");
  }

  const configured = firebaseConfiguredOnServer();
  // Checking signup_enabled against a product API that cannot even authenticate a token is
  // pointless -- and the deployment-not-configured message takes precedence when both are true.
  if (configured && !(await signupEnabled())) {
    redirect("/sign-in?signup=closed");
  }

  return (
    <div className="auth__panel">
      <h1 className="auth__title">
        Start meshing in <em>hours</em>
      </h1>
      <p className="auth__sub">
        Every new account starts with signup credits. No card, no sales call.
      </p>
      {configured ? (
        <>
          <SignUpForm />
          <div className="auth__alt">
            <Link href="/sign-in">Already have an account? Sign in</Link>
          </div>
        </>
      ) : (
        // Same rule as /sign-in: a console whose Identity Platform project was never set up
        // must say so rather than presenting a form that fails on every submit.
        <div className="auth__error" role="status">
          Sign-in is not configured for this deployment.
        </div>
      )}
    </div>
  );
}
