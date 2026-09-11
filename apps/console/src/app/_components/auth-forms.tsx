"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import {
  createUserWithEmailAndPassword,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  updateProfile,
} from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";
import {
  explainSessionError,
  messageForSignIn,
  messageForSignUp,
} from "@/app/_components/auth-form-messages";
import { resumeInterruptedSignup } from "@/app/_components/signup-recovery";

function Field({
  autoComplete,
  label,
  minLength,
  name,
  type,
}: {
  autoComplete: string;
  label: string;
  minLength?: number;
  name: string;
  type: string;
}) {
  return (
    <label className="auth__field">
      <span className="label">{label}</span>
      <input
        autoComplete={autoComplete}
        className="auth__input"
        minLength={minLength}
        name={name}
        required
        type={type}
      />
    </label>
  );
}

export function SignUpForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setError(null);
    const name = String(formData.get("name") ?? "").trim();
    const company = String(formData.get("company") ?? "").trim();
    const email = String(formData.get("email") ?? "");
    const password = String(formData.get("password") ?? "");
    try {
      const credential = await createUserWithEmailAndPassword(firebaseAuth(), email, password);
      // BEFORE the token is minted, so token.name carries the display name into users.name with
      // no API change. A token minted first would carry the address as the name forever.
      if (name) {
        await updateProfile(credential.user, { displayName: name });
      }
      await sendEmailVerification(credential.user);
      const idToken = await credential.user.getIdToken(true);
      const result = await signIn("credentials", {
        idToken,
        organizationName: company,
        redirect: false,
      });
      if (result?.error) {
        setError(explainSessionError(result.error, "Could not create the account. Try again."));
        return;
      }
      // The account exists but the address is not proven, so the dashboard layout will bounce
      // straight to /verify-email. Going there directly saves the user a redirect they would
      // otherwise see as a flash of the console they cannot use.
      router.push("/verify-email");
      router.refresh();
    } catch (cause) {
      // A PART-WAY SIGN-UP IS RESUMABLE, and this is the only place it can be resumed from.
      // `createUserWithEmailAndPassword` is the first of five steps; if a later one failed on an
      // earlier attempt the credential already exists, every retry is refused with
      // `auth/email-already-in-use`, and signing in instead would finish the account WITHOUT the
      // company -- which names the organisation after the address, permanently. The company is
      // still in this form, so the recovery happens here.
      if ((cause as { code?: string })?.code === "auth/email-already-in-use") {
        const outcome = await resumeInterruptedSignup(
          {
            signInWithPassword: async (address, secret) => {
              try {
                const resumed = await signInWithEmailAndPassword(firebaseAuth(), address, secret);
                return {
                  setDisplayName: (displayName) =>
                    updateProfile(resumed.user, { displayName }),
                  getIdToken: (force) => resumed.user.getIdToken(force),
                };
              } catch {
                // Not ours to resume: the address belongs to somebody else, or the password is
                // wrong. Falls through to the ordinary "already has an account" message.
                return null;
              }
            },
            mintSession: async (idToken, organizationName) => {
              const result = await signIn("credentials", {
                idToken,
                organizationName,
                redirect: false,
              });
              return { error: result?.error ?? undefined };
            },
          },
          { email, password, name, company },
        );
        if (outcome === "resumed") {
          router.push("/verify-email");
          router.refresh();
          return;
        }
      }
      setError(messageForSignUp(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} className="auth__form">
      <Field autoComplete="name" label="Full name" name="name" type="text" />
      <Field autoComplete="organization" label="Company" name="company" type="text" />
      <Field autoComplete="email" label="Work email" name="email" type="email" />
      <Field
        autoComplete="new-password"
        label="Password"
        minLength={8}
        name="password"
        type="password"
      />
      {error ? (
        <div aria-live="polite" className="auth__error" role="status">
          {error}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Creating account…" : "Create account"}
      </button>
    </form>
  );
}

export function SignInForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setError(null);
    try {
      const credential = await signInWithEmailAndPassword(
        firebaseAuth(),
        String(formData.get("email") ?? ""),
        String(formData.get("password") ?? ""),
      );
      const idToken = await credential.user.getIdToken();
      const result = await signIn("credentials", { idToken, redirect: false });
      if (result?.error) {
        setError(explainSessionError(result.error, "Could not sign in. Try again."));
        return;
      }
      router.push("/");
      router.refresh();
    } catch (cause) {
      setError(messageForSignIn(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} className="auth__form">
      <Field autoComplete="email" label="Email" name="email" type="email" />
      <Field autoComplete="current-password" label="Password" name="password" type="password" />
      {error ? (
        <div aria-live="polite" className="auth__error" role="status">
          {error}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export function ForgotPasswordForm() {
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setMessage(null);
    try {
      await sendPasswordResetEmail(firebaseAuth(), String(formData.get("email") ?? ""));
    } catch {
      // Deliberately swallowed. sendPasswordResetEmail does not distinguish "no such account"
      // from success, and this page must not either -- reporting anything else would tell an
      // attacker whether the address has an account, which Firebase itself refuses to do.
    } finally {
      setBusy(false);
      setMessage("If that address has an account, a reset link is on its way.");
    }
  }

  return (
    <form action={submit} className="auth__form">
      <Field autoComplete="email" label="Email" name="email" type="email" />
      {message ? (
        <div aria-live="polite" className="auth__error auth__note" role="status">
          {message}
        </div>
      ) : null}
      <button className="btn btn--gold" disabled={busy} type="submit">
        {busy ? "Sending…" : "Send reset link"}
      </button>
    </form>
  );
}
