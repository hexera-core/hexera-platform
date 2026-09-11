import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { VerifyEmailForm } from "@/app/_components/verify-email-form";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function VerifyEmailPage() {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!session || !ownerId) {
    redirect("/sign-in");
  }
  // Already proven. Sitting on this page with a verified session is a dead end, so it sends the
  // user where they were going.
  if (session.emailVerified) {
    redirect("/");
  }

  return (
    <div className="auth__panel">
      <h1 className="auth__title">
        Check your <em>inbox</em>
      </h1>
      <p className="auth__sub">
        A verification link is on its way to <strong>{ownerId}</strong>. Open it, then continue —
        your account and its signup credits are already waiting.
      </p>
      <VerifyEmailForm email={ownerId} />
      <p className="auth__foot">
        The sender is <code>noreply</code> at our Identity Platform domain, so check spam if it is
        not there in a minute. Wrong address? Sign out and create the account again.
      </p>
      <SignOutButton />
    </div>
  );
}
