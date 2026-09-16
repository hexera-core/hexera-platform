"use client";

import { useActionState } from "react";

import { fleetControlAction } from "./actions";
import type { ActionResult } from "./operations";

const IDLE: ActionResult = { ok: true };

// Every control on the Fleet page posts through this. It exists so the pending state, the refusal
// message and the "what just happened" message are rendered the same way everywhere - an operator
// should not have to learn a different feedback shape per button.
//
// The result of a refused change is shown in the alert tone, and the result of an accepted one in
// the notice tone, because the two are read differently: the first means nothing happened, and
// the second means something is now happening that this page has not caught up with yet.
export function ActionForm({
  children,
  danger = false,
  operation,
  submitLabel,
}: {
  children?: React.ReactNode;
  danger?: boolean;
  operation: string;
  submitLabel: string;
}) {
  const [state, formAction, pending] = useActionState(fleetControlAction, IDLE);

  return (
    <form action={formAction} className="admin-form">
      <input name="operation" type="hidden" value={operation} />
      {children}
      <button
        className={danger ? "admin-button admin-button-danger" : "admin-button"}
        disabled={pending}
        type="submit"
      >
        {pending ? "Working…" : submitLabel}
      </button>
      {state.error ? <p className="admin-alert admin-form-result">{state.error}</p> : null}
      {state.ok && state.message ? (
        <p className="admin-notice admin-form-result">{state.message}</p>
      ) : null}
    </form>
  );
}

export function NumberField({
  defaultValue,
  hint,
  label,
  name,
  max,
}: {
  defaultValue?: number | null;
  hint?: string;
  label: string;
  max?: number;
  name: string;
}) {
  return (
    <label className="admin-field-input">
      <span>{label}</span>
      <input
        defaultValue={defaultValue ?? ""}
        inputMode="numeric"
        max={max}
        min={0}
        name={name}
        step={1}
        type="number"
      />
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}
