"use client";

import { useActionState } from "react";

import { claimRepair, decideRepair, REPAIR_FORM_IDLE } from "./actions";

// THE REFUSAL IS PART OF THE FORM, NOT A PAGE. Two operators work one queue, so a decision that
// arrives a moment late is an ordinary 409 rather than a fault - and an operator who has just
// typed the reason a customer will read must get that refusal back WITH what they typed, not an
// error page that discards it.
//
// Labels come in as props: this is a client component, and the label helpers live beside the
// admin API reader, which is server-only.

export function RepairDecisionForm({
  decision,
  jobId,
  needsReason,
  strategies,
  submitLabel,
}: {
  decision: string;
  jobId: string;
  needsReason: boolean;
  strategies?: readonly string[];
  submitLabel: string;
}) {
  const [state, formAction, pending] = useActionState(decideRepair, REPAIR_FORM_IDLE);

  return (
    <form action={formAction} className="admin-form">
      <input name="job_id" type="hidden" value={jobId} />
      <input name="decision" type="hidden" value={decision} />
      {strategies && strategies.length > 0 ? (
        <label>
          Strategy
          <select defaultValue={state.strategy || strategies[0]} name="strategy">
            {strategies.map((strategy) => (
              <option key={strategy} value={strategy}>
                {strategy}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {needsReason ? (
        <label>
          Reason (the customer is told this)
          {/* defaultValue carries a refused submission's own text back, which is the whole
              point: the operator corrects the decision rather than retyping the reason. */}
          <input
            defaultValue={state.reason ?? ""}
            maxLength={2000}
            name="reason"
            required
            type="text"
          />
        </label>
      ) : null}
      <label>
        Notes
        <input defaultValue={state.notes ?? ""} maxLength={4000} name="notes" type="text" />
      </label>
      <button disabled={pending} type="submit">
        {pending ? "Working…" : submitLabel}
      </button>
      {state.error ? <p className="admin-alert admin-form-result">{state.error}</p> : null}
    </form>
  );
}

export function RepairClaimForm({
  holder,
  jobId,
}: {
  holder: string | null;
  jobId: string;
}) {
  const [state, formAction, pending] = useActionState(claimRepair, REPAIR_FORM_IDLE);

  return (
    <form action={formAction} className="admin-form">
      <input name="job_id" type="hidden" value={jobId} />
      <input name="release" type="hidden" value={holder ? "1" : "0"} />
      {/* who holds it, so a release can be refused unless it is your own claim */}
      <input name="assigned_operator" type="hidden" value={holder ?? ""} />
      <button disabled={pending} type="submit">
        {pending ? "Working…" : holder ? "Release it" : "Claim it"}
      </button>
      {state.error ? <p className="admin-alert admin-form-result">{state.error}</p> : null}
    </form>
  );
}
