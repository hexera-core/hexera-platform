import { recordAdminAction, type AdminAuditEntry } from "@/lib/audit";
import type { IapActor } from "@/lib/auth/iap";
import {
  assignRepairJob,
  decideRepairJob,
  REPAIR_DECISIONS_NEEDING_REASON,
} from "@/lib/admin/read";

// WHO DECIDED, ESTABLISHED RATHER THAN TYPED.
//
// The product API records the actor on a repair decision as a plain string, because the shared
// admin credential it authenticates proves staff access and names nobody. This console can do
// better: every write here runs behind IAP, so the actor is the VERIFIED identity from the
// assertion and is never read from the form. An operator cannot decide on somebody else's behalf
// by editing a field, and the audit on both sides names the same person.

export type RepairActionResult = {
  error?: string;
  ok: boolean;
};

type RepairMutationDeps = {
  actor: IapActor;
  assign?: typeof assignRepairJob;
  audit?: (entry: AdminAuditEntry) => void;
  decide?: typeof decideRepairJob;
};

function requiredString(form: FormData, field: string): string {
  const raw = form.get(field);
  if (typeof raw !== "string" || raw.trim() === "") {
    throw new Error(`${field} is required.`);
  }
  return raw.trim();
}

function optionalString(form: FormData, field: string): string {
  const raw = form.get(field);
  return typeof raw === "string" ? raw.trim() : "";
}

export async function applyRepairDecision(
  form: FormData,
  deps: RepairMutationDeps,
): Promise<RepairActionResult> {
  const audit = deps.audit ?? recordAdminAction;
  const decide = deps.decide ?? decideRepairJob;

  try {
    const jobId = requiredString(form, "job_id");
    const decision = requiredString(form, "decision");
    const reason = optionalString(form, "reason");
    // REFUSED HERE, not discovered as a 422 after the operator has lost what they typed. The API
    // refuses these too, and must: this check is the better message, not the security boundary.
    if (REPAIR_DECISIONS_NEEDING_REASON.includes(decision) && reason === "") {
      throw new Error(
        `'${decision}' stops or holds a customer's job, so it must state a reason. ` +
          "That reason is what the customer is eventually told.",
      );
    }

    const result = await decide({
      actor: deps.actor.email,
      decision,
      jobId,
      notes: optionalString(form, "notes"),
      reason,
      strategy: optionalString(form, "strategy"),
    });
    if (result.error) return { error: result.error, ok: false };

    audit({
      action: `repair.decide.${decision}`,
      actor: deps.actor.email,
      after: { decision, reason, strategy: optionalString(form, "strategy") },
      resource: `repair-job/${jobId}`,
    });
    return { ok: true };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), ok: false };
  }
}

export async function applyRepairAssignment(
  form: FormData,
  deps: RepairMutationDeps,
): Promise<RepairActionResult> {
  const audit = deps.audit ?? recordAdminAction;
  const assign = deps.assign ?? assignRepairJob;

  try {
    const jobId = requiredString(form, "job_id");
    // CLAIM for myself, RELEASE back to the queue. There is no "assign to someone else" on this
    // form: reassigning another person's work is a lead's act and wants its own surface with its
    // own audit, not a text field on the row everybody can see.
    const release = optionalString(form, "release") === "1";
    const operator = release ? "" : deps.actor.email;

    const result = await assign({ claim: !release, jobId, operator });
    if (result.error) return { error: result.error, ok: false };

    audit({
      action: release ? "repair.release" : "repair.claim",
      actor: deps.actor.email,
      after: { assigned_operator: operator || null },
      resource: `repair-job/${jobId}`,
    });
    return { ok: true };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), ok: false };
  }
}
