"use client";

import { useActionState, useEffect, useState } from "react";
import type { ReactNode } from "react";

export interface ActionResult {
  ok: boolean;
  message: string;
  skipped?: { contactId: number; company: string; reasons: string[] }[];
}

type Action = (formData: FormData) => Promise<ActionResult>;

/**
 * Wraps a server action and renders whatever it returns.
 *
 * Actions in this app return `{ok, message}` rather than throwing, because
 * most failures here are expected outcomes a person needs to read — "already
 * suppressed", "not verified yet", "type SEND LIVE to confirm" — not
 * exceptions. Surfacing them inline beats a thrown error the UI has to guess
 * how to present.
 */
export function ActionForm({
  action,
  children,
  className,
}: {
  action: Action;
  children: ReactNode;
  className?: string;
}) {
  const [state, formAction, pending] = useActionState(
    async (_prev: ActionResult | null, formData: FormData) => action(formData),
    null,
  );

  // Confirmations are a notification, not a status. Left on screen they read
  // as the current state of the thing they sit under, which is wrong the
  // moment anything else changes, so they clear themselves. Failures stay:
  // those are something you still have to deal with.
  // DERIVED, not synchronised. Setting state synchronously inside an effect to mirror another
  // piece of state is a cascading render, and the lint rule that catches it is right: visibility
  // is a function of "which result is this, and has it been dismissed", not an independent flag.
  // The only setState left runs inside the timer, which is genuinely asynchronous.
  const [dismissed, setDismissed] = useState<ActionResult | null>(null);
  useEffect(() => {
    if (!state?.ok) return;
    const timer = setTimeout(() => setDismissed(state), 4000);
    return () => clearTimeout(timer);
  }, [state]);

  const showing = state && state !== dismissed;

  return (
    <form action={formAction} className={className}>
      <fieldset disabled={pending} className="border-0 p-0 m-0 contents">
        {children}
      </fieldset>

      {showing && (
        <p
          className="text-[0.75rem] mt-2 leading-snug transition-opacity duration-300"
          style={{ color: state.ok ? "var(--color-muted)" : "var(--color-crimson-soft)" }}
          role="status"
        >
          {state.message}
        </p>
      )}

      {showing && state?.skipped?.length ? (
        <details className="mt-2">
          <summary className="t-label cursor-pointer">
            {state.skipped.length} held back, and why
          </summary>
          <ul className="list-none m-0 mt-2 p-0 flex flex-col gap-1 max-h-48 overflow-y-auto scroll-y">
            {state.skipped.map((item) => (
              <li key={item.contactId} className="text-[0.6875rem] text-[var(--color-muted)]">
                <span className="text-[var(--color-ink)]">{item.company}</span>: {item.reasons.join("; ")}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </form>
  );
}

export function SubmitButton({
  children,
  variant = "default",
  ...props
}: {
  children: ReactNode;
  variant?: "default" | "primary" | "danger";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const cls =
    variant === "primary" ? "btn btn-primary" : variant === "danger" ? "btn btn-danger" : "btn";
  return (
    <button type="submit" className={cls} {...props}>
      {children}
    </button>
  );
}
