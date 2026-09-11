"use client";

import { useState } from "react";
import { updatePassword, updateProfile } from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";

export function AccountForm({ name }: { name: string }) {
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setNote(null);
    const user = firebaseAuth().currentUser;
    if (!user) {
      setNote("Your session expired. Sign in again.");
      setBusy(false);
      return;
    }
    try {
      const displayName = String(formData.get("name") ?? "").trim();
      if (displayName && displayName !== name) {
        await updateProfile(user, { displayName });
      }
      const password = String(formData.get("password") ?? "");
      if (password) {
        await updatePassword(user, password);
      }
      setNote("Saved.");
    } catch (cause) {
      // Identity Platform requires a RECENT sign-in for a password change and says so with its
      // own code. Anything else is reported without a diagnosis this cannot know.
      const code = (cause as { code?: string })?.code ?? "";
      setNote(
        code === "auth/requires-recent-login"
          ? "Sign out and back in, then change your password."
          : "Could not save that. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} className="auth__form">
      <label className="auth__field">
        <span className="label">Display name</span>
        <input className="auth__input" defaultValue={name} name="name" type="text" />
      </label>
      <label className="auth__field">
        <span className="label">New password</span>
        <input
          autoComplete="new-password"
          className="auth__input"
          minLength={8}
          name="password"
          type="password"
        />
      </label>
      {note ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {note}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
