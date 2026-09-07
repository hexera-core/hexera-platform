import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { GoogleSignInButton } from "@/app/_components/auth-buttons";

export default async function SignInPage() {
  const session = await auth();
  if (session) {
    redirect("/");
  }

  return (
    <main className="auth-page">
      <section className="auth-panel" aria-labelledby="sign-in-title">
        <p className="auth-kicker">console.hexera.ai</p>
        <h1 id="sign-in-title">Sign in to Hexera</h1>
        <p className="auth-copy">Use your Google account to open the simulation console.</p>
        <GoogleSignInButton />
      </section>
    </main>
  );
}
