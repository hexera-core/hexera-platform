import { recordAdminAction, type AdminAuditEntry } from "@/lib/audit";
import type { IapActor } from "@/lib/auth/iap";
import { grantCredits } from "@/lib/admin/read";
import { raiseInvoice } from "@/lib/billing/read";

export type CustomerActionResult = {
  error?: string;
  ok: boolean;
};

type CustomerMutationDeps = {
  actor: IapActor;
  audit?: (entry: AdminAuditEntry) => void;
  grant?: typeof grantCredits;
  invoice?: typeof raiseInvoice;
};

function requiredString(form: FormData, field: string): string {
  const raw = form.get(field);
  if (typeof raw !== "string" || raw.trim() === "") {
    throw new Error(`${field} is required.`);
  }
  return raw.trim();
}

function requiredNumber(form: FormData, field: string): number {
  const raw = requiredString(form, field);
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    throw new Error(`${field} must be a number; got '${raw}'.`);
  }
  return value;
}

export async function applyCreditGrant(
  form: FormData,
  deps: CustomerMutationDeps,
): Promise<CustomerActionResult> {
  const audit = deps.audit ?? recordAdminAction;
  const grant = deps.grant ?? grantCredits;

  try {
    const organizationId = requiredString(form, "organization_id");
    const amount = requiredNumber(form, "amount");
    const operationId = requiredString(form, "operation_id");
    const reason = requiredString(form, "reason");

    const result = await grant({ amount, operationId, organizationId, reason });
    if (result.error || !result.data) {
      return { error: result.error ?? "Credit grant failed.", ok: false };
    }

    audit({
      action: "customer.credit_grant.create",
      actor: deps.actor.email,
      after: { amount, balance: result.data.balance, reason },
      resource: organizationId,
    });
    return { ok: true };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), ok: false };
  }
}

export async function applyInvoiceRaise(
  form: FormData,
  deps: CustomerMutationDeps,
): Promise<CustomerActionResult> {
  const audit = deps.audit ?? recordAdminAction;
  const invoice = deps.invoice ?? raiseInvoice;

  try {
    const organizationId = requiredString(form, "organization_id");
    const amount = requiredNumber(form, "amount");
    const currency = requiredString(form, "currency").toLowerCase();
    const daysUntilDue = requiredNumber(form, "days_until_due");
    const description = requiredString(form, "description");
    const operationId = requiredString(form, "operation_id");

    const result = await invoice({
      amount,
      currency,
      daysUntilDue,
      description,
      operationId,
      organizationId,
    });
    if (result.error || !result.data) {
      return { error: result.error ?? "Invoice creation failed.", ok: false };
    }

    audit({
      action: "customer.invoice.raise",
      actor: deps.actor.email,
      after: {
        amount,
        currency,
        daysUntilDue,
        description,
        invoiceId: result.data.invoice.id,
      },
      resource: organizationId,
    });
    return { ok: true };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), ok: false };
  }
}
