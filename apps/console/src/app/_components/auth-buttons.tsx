import type { CSSProperties } from "react";
import { AuthError } from "next-auth";
import { redirect } from "next/navigation";

import { signIn, signOut } from "@/auth";

const signInFormStyle: CSSProperties = {
  display: "grid",
  gap: "10px",
  margin: "18px auto 0",
  maxWidth: "360px",
  textAlign: "left",
};

const labelStyle: CSSProperties = {
  color: "var(--text-muted)",
  display: "grid",
  fontFamily: "var(--sans)",
  fontSize: "12px",
  gap: "5px",
};

const inputStyle: CSSProperties = {
  background: "var(--surface-1)",
  border: "1px solid var(--border)",
  borderRadius: "3px",
  color: "var(--text)",
  fontFamily: "var(--sans)",
  fontSize: "14px",
  lineHeight: "1.5",
  padding: "11px 14px",
};

const errorStyle: CSSProperties = {
  color: "var(--warn)",
  fontFamily: "var(--mono)",
  fontSize: "11px",
  lineHeight: "1.5",
  textAlign: "center",
};

export function EmailPasswordSignInForm({ hasError = false }: { hasError?: boolean }) {
  return (
    <form
      action={async (formData) => {
        "use server";
        try {
          await signIn("credentials", formData);
        } catch (error) {
          if (error instanceof AuthError) {
            redirect("/sign-in?error=CredentialsSignin");
          }

          throw error;
        }
      }}
      style={signInFormStyle}
    >
      <input name="redirectTo" type="hidden" value="/" />
      <label style={labelStyle}>
        Email
        <input
          autoComplete="email"
          name="email"
          required
          style={inputStyle}
          type="email"
        />
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
      {hasError ? (
        <div aria-live="polite" role="status" style={errorStyle}>
          Invalid email or password.
        </div>
      ) : null}
      <button id="upload-btn" type="submit">
        Sign in
      </button>
    </form>
  );
}

export function SignOutButton() {
  return (
    <form
      action={async () => {
        "use server";
        await signOut({ redirectTo: "/sign-in" });
      }}
    >
      <button className="chip" type="submit">
        Sign out
      </button>
    </form>
  );
}
