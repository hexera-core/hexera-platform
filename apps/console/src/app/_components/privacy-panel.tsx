"use client";

import { useState } from "react";
import { signOut } from "next-auth/react";
import { deleteUser } from "firebase/auth";

import { confirmationMatches, erasureOutcomeMessage } from "@/app/_components/privacy-messages";
import { firebaseAuth } from "@/lib/firebase/client";

export function PrivacyPanel({ email }: { email: string }) {
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState("");

  async function exportData() {
    setBusy(true);
    setNote(null);
    try {
      const response = await fetch("/api/v1/account/export");
      if (!response.ok) {
        setNote("Could not prepare your export just now. Try again.");
        return;
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "hexera-account-export.json";
      link.click();
      URL.revokeObjectURL(url);
    } catch {
      setNote("Could not prepare your export just now. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function erase() {
    if (!confirmationMatches(typed, email)) {
      setNote(erasureOutcomeMessage(400));
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const response = await fetch("/api/v1/account/erasure", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ confirm_email: typed.trim() }),
      });
      if (response.status !== 202) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        setNote(erasureOutcomeMessage(response.status, body.detail));
        return;
      }
      // THE SIGN-IN RECORD GOES TOO. The API has already removed everything that names this
      // person; the Identity Platform account is the console's to delete, because only this
      // session can. A stale session makes Identity Platform refuse the delete - the account
      // DATA is gone either way, but the sign-in record then remains, and finishing silently
      // would leave the person believing otherwise. They are told, and sign out themselves.
      const user = firebaseAuth().currentUser;
      const signInRecordRemoved = user
        ? await deleteUser(user).then(() => true, () => false)
        : false;
      if (!signInRecordRemoved) {
        setNote(
          "Your account data has been deleted. Your sign-in record could not be removed from " +
            "this session - sign in once more and delete the account again, or contact support " +
            "to remove it.",
        );
        return;
      }
      await signOut({ redirectTo: "/sign-in" });
    } catch {
      setNote(erasureOutcomeMessage(0));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth__form">
      <div>
        <p className="page__sub">
          Download everything Hexera holds about you and your organisation as one JSON file.
        </p>
        <button className="btn" disabled={busy} onClick={exportData} type="button">
          Download my data
        </button>
      </div>
      <div>
        <p className="page__sub">
          Delete your account and organisation. API keys stop working immediately, conversations
          and your profile are removed, and uploaded files and results are deleted within a day.
          Billing records are kept as the law requires. This cannot be undone.
        </p>
        <label className="auth__field">
          <span className="label">Type {email} to confirm</span>
          <input
            autoComplete="off"
            className="auth__input"
            name="confirm_email"
            onChange={(event) => setTyped(event.target.value)}
            type="email"
            value={typed}
          />
        </label>
        <button
          className="btn btn--ghost"
          disabled={busy || !confirmationMatches(typed, email)}
          onClick={erase}
          type="button"
        >
          {busy ? "Working…" : "Delete my account"}
        </button>
      </div>
      {note ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {note}
        </div>
      ) : null}
    </div>
  );
}
