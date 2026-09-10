import type { CSSProperties } from "react";

// Shared by every auth form (sign-up, sign-in, forgot-password) in auth-forms.tsx so the four
// cannot drift apart visually. This lives in its own directive-free module rather than inside
// auth-buttons.tsx: auth-buttons.tsx also defines SignOutButton, whose form action carries an
// inline "use server" annotation, and Next.js refuses to build a module that both defines an
// inline Server Action and is reachable from a "use client" file's import graph (auth-forms.tsx
// needs these constants and is itself a Client Component).
export const signInFormStyle: CSSProperties = {
  display: "grid",
  gap: "10px",
  margin: "18px auto 0",
  maxWidth: "360px",
  textAlign: "left",
};

export const labelStyle: CSSProperties = {
  color: "var(--text-muted)",
  display: "grid",
  fontFamily: "var(--sans)",
  fontSize: "12px",
  gap: "5px",
};

export const inputStyle: CSSProperties = {
  background: "var(--surface-1)",
  border: "1px solid var(--border)",
  borderRadius: "3px",
  color: "var(--text)",
  fontFamily: "var(--sans)",
  fontSize: "14px",
  lineHeight: "1.5",
  padding: "11px 14px",
};

export const errorStyle: CSSProperties = {
  color: "var(--warn)",
  fontFamily: "var(--mono)",
  fontSize: "11px",
  lineHeight: "1.5",
  textAlign: "center",
};
