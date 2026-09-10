"use client";

import { useState, type CSSProperties } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "next-auth/react";
import {
  createUserWithEmailAndPassword,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
} from "firebase/auth";

import { firebaseAuth } from "@/lib/firebase/client";
import { errorStyle, inputStyle, labelStyle, signInFormStyle } from "@/app/_components/auth-form-styles";
import { explainSessionError, messageForSignIn, messageForSignUp } from "@/app/_components/auth-form-messages";

export function SignUpForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(formData: FormData) {
    setBusy(true);
    setError(null);
    const email = String(formData.get("email") ?? "");
    const password = String(formData.get("password") ?? "");
    try {
      const credential = await createUserWithEmailAndPassword(firebaseAuth(), email, password);
      // Sent before the session exists, so a signup that fails at the API still leaves the
      // person with a verifiable address rather than an account they cannot prove is theirs.
      await sendEmailVerification(credential.user);
      const idToken = await credential.user.getIdToken();
      const result = await signIn("credentials", { idToken, redirect: false });
      if (result?.error) {
        setError(explainSessionError(result.error, "Could not create the account. Try again."));
        return;
      }
      // A push (rather than a hard navigation) plus a refresh, so the server components on "/"
      // re-render against the session cookie signIn() just set instead of a cached RSC payload.
      router.push("/");
      router.refresh();
    } catch (cause) {
      // Firebase's own codes are the only place these distinctions are safe to make: it applies
      // its own enumeration protection, so echoing its message does not tell an attacker
      // anything it would not tell them directly.
      setError(messageForSignUp(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form action={submit} style={signInFormStyle}>
      <label style={labelStyle}>
        Email
        <input autoComplete="email" name="email" required style={inputStyle} type="email" />
      </label>
      <label style={labelStyle}>
        Password
        <input
          autoComplete="new-password"
          minLength={8}
          name="password"
          required
          style={inputStyle}
          type="password"
        />
      </label>
      {error ? (
        <div aria-live="polite" role="status" style={errorStyle}>
          {error}
        </div>
      ) : null}
      <button disabled={busy} id="upload-btn" type="submit">
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
    const email = String(formData.get("email") ?? "");
    const password = String(formData.get("password") ?? "");
    try {
      const credential = await signInWithEmailAndPassword(firebaseAuth(), email, password);
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
    <form action={submit} style={signInFormStyle}>
      <label style={labelStyle}>
        Email
        <input autoComplete="email" name="email" required style={inputStyle} type="email" />
      </label>
      <label style={labelStyle}>
        Password
        <input
          autoComplete="current-password"
          name="password"
          required
          style={inputStyle}
          type="password"
        />
      </label>
      {error ? (
        <div aria-live="polite" role="status" style={errorStyle}>
          {error}
        </div>
      ) : null}
      <button disabled={busy} id="upload-btn" type="submit">
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
    const email = String(formData.get("email") ?? "");
    try {
      await sendPasswordResetEmail(firebaseAuth(), email);
    } catch {
      // Deliberately swallowed. sendPasswordResetEmail does not distinguish "no such account"
      // from success, and this page must not either -- reporting anything else here would tell
      // an attacker whether the address has an account, which Firebase itself refuses to do.
    } finally {
      setBusy(false);
      setMessage("If that address has an account, a reset link is on its way.");
    }
  }

  return (
    <form action={submit} style={signInFormStyle}>
      <label style={labelStyle}>
        Email
        <input autoComplete="email" name="email" required style={inputStyle} type="email" />
      </label>
      {message ? (
        <div aria-live="polite" role="status" style={errorStyle}>
          {message}
        </div>
      ) : null}
      <button disabled={busy} id="upload-btn" type="submit">
        {busy ? "Sending…" : "Send reset link"}
      </button>
    </form>
  );
}

const bannerStyle: CSSProperties = {
  alignItems: "center",
  background: "var(--warn-soft)",
  border: "1px solid var(--warn)",
  borderRadius: "3px",
  color: "var(--text)",
  display: "flex",
  fontFamily: "var(--sans)",
  fontSize: "12px",
  gap: "12px",
  justifyContent: "space-between",
  margin: "0 auto",
  maxWidth: "720px",
  padding: "10px 14px",
};

export function VerifyEmailBanner() {
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function resend() {
    setBusy(true);
    try {
      const user = firebaseAuth().currentUser;
      if (user) {
        await sendEmailVerification(user);
        setSent(true);
      }
    } catch {
      // Best-effort. Nothing costly is gated on verification yet (see design §4), so a failed
      // resend is not worth surfacing as an error -- the control stays available to retry.
    } finally {
      setBusy(false);
    }
  }

  return (
    <div aria-live="polite" role="status" style={bannerStyle}>
      <span>Verify your email address to secure your account.</span>
      <button disabled={busy || sent} onClick={resend} type="button">
        {sent ? "Verification email sent" : busy ? "Sending…" : "Resend verification email"}
      </button>
    </div>
  );
}
