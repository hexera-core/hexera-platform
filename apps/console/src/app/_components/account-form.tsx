"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import { updatePassword, updateProfile } from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";

export function AccountForm({ name }: { name: string }) {
  const router = useRouter();
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
      const renamed = Boolean(displayName) && displayName !== name;
      if (renamed) {
        await updateProfile(user, { displayName });
      }
      const password = String(formData.get("password") ?? "");
      if (password) {
        await updatePassword(user, password);
      }
      if (renamed && !(await adoptNewName(user))) {
        // "Saved." here would be a lie the user could see: the write landed in Identity
        // Platform, but everything that renders a name reads the session, and the session still
        // says the old one.
        setNote("Saved, but this page will keep showing the old name until you sign in again.");
        return;
      }
      if (renamed) {
        router.refresh();
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

/** Re-mint the Auth.js session against a token that carries the NEW display name.
 *
 * `updateProfile` writes Identity Platform and nothing else. The session's name arrives once,
 * from `authorizeFirebaseSession`'s response at sign-in, and the jwt callback re-reads it only
 * when a fresh `signIn("credentials")` supplies a `user` - so without this the sidebar, this page
 * and /settings/organization all keep the old name indefinitely while the form says "Saved."
 *
 * `getIdToken(true)` is not optional. The SDK hands back the cached token otherwise, whose `name`
 * claim is still the old one, and the re-mint would faithfully restore exactly what it replaced.
 * The same forced refresh, for the same reason, as verify-email-refresh.ts. It is also what
 * carries the new name to the product API, whose `record_login` writes it to `users.name`.
 */
async function adoptNewName(user: { getIdToken: (forceRefresh: boolean) => Promise<string> }) {
  try {
    const idToken = await user.getIdToken(true);
    const result = await signIn("credentials", { idToken, redirect: false });
    return !result?.error;
  } catch {
    return false;
  }
}
