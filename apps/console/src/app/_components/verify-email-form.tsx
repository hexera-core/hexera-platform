"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { sendEmailVerification } from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";
import { refreshVerifiedSession } from "@/app/_components/verify-email-refresh";

export function VerifyEmailForm({ email }: { email: string }) {
  const router = useRouter();
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function resend() {
    setBusy(true);
    setNote(null);
    try {
      const user = firebaseAuth().currentUser;
      if (!user) {
        setNote("Your session expired. Sign in again to resend the link.");
        return;
      }
      await sendEmailVerification(user);
      setNote(`Sent again to ${email}. Identity Platform throttles repeats, so give it a minute.`);
    } catch {
      // Best effort, and deliberately not diagnosed: the SDK's throttling error and a transient
      // network failure are indistinguishable from here, and the control stays available.
      setNote("Could not send just now. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  async function checkAgain() {
    setBusy(true);
    setNote(null);
    try {
      const outcome = await refreshVerifiedSession({
        currentUser: () => firebaseAuth().currentUser,
        signIn: async (idToken) => {
          const result = await signIn("credentials", { idToken, redirect: false });
          return { error: result?.error ?? undefined };
        },
      });
      if (outcome === "verified") {
        router.push("/");
        router.refresh();
        return;
      }
      setNote(
        outcome === "signed-out"
          ? "Your session expired. Sign in again to continue."
          : "That address is not verified yet. Open the link in your inbox, then check again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="auth__form">
        <button className="btn btn--gold" disabled={busy} onClick={checkAgain} type="button">
          {busy ? "Checking…" : "I've verified — continue"}
        </button>
        <button className="btn" disabled={busy} onClick={resend} type="button">
          Resend the link
        </button>
      </div>
      {note ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {note}
        </div>
      ) : null}
    </>
  );
}
