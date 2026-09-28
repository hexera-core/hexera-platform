import assert from "node:assert/strict";
import test from "node:test";

import { applyCreditGrant, applyInvoiceRaise } from "./operations";

const ACTOR = { email: "operator@hexera.ai", subject: "accounts.google.com:1234" };

test("credit grants write one audit line naming the verified actor", async () => {
  const form = new FormData();
  form.set("organization_id", "org-1");
  form.set("amount", "1500");
  form.set("operation_id", "grant-op-1");
  form.set("reason", "launch goodwill");
  const audited: unknown[] = [];

  const result = await applyCreditGrant(form, {
    actor: ACTOR,
    audit: (entry) => audited.push(entry),
    grant: async (input) => {
      assert.deepEqual(input, {
        amount: 1500,
        operationId: "grant-op-1",
        organizationId: "org-1",
        reason: "launch goodwill",
      });
      return { data: { balance: 2500 }, error: null };
    },
  });

  assert.deepEqual(result, { ok: true });
  assert.equal(audited.length, 1);
  assert.deepEqual(audited[0], {
    action: "customer.credit_grant.create",
    actor: ACTOR.email,
    after: { amount: 1500, balance: 2500, reason: "launch goodwill" },
    resource: "org-1",
  });
});

test("failed credit grants are not audited as successful actions", async () => {
  const form = new FormData();
  form.set("organization_id", "org-1");
  form.set("amount", "1500");
  form.set("operation_id", "grant-op-1");
  form.set("reason", "launch goodwill");
  const audited: unknown[] = [];

  const result = await applyCreditGrant(form, {
    actor: ACTOR,
    audit: (entry) => audited.push(entry),
    grant: async () => ({ data: null, error: "refused" }),
  });

  assert.deepEqual(result, { error: "refused", ok: false });
  assert.deepEqual(audited, []);
});

test("manual invoices write one audit line naming the verified actor", async () => {
  const form = new FormData();
  form.set("organization_id", "org-1");
  form.set("amount", "250000");
  form.set("currency", "USD");
  form.set("days_until_due", "14");
  form.set("description", "Enterprise launch invoice");
  form.set("operation_id", "invoice-op-1");
  const audited: unknown[] = [];

  const result = await applyInvoiceRaise(form, {
    actor: ACTOR,
    audit: (entry) => audited.push(entry),
    invoice: async (input) => {
      assert.deepEqual(input, {
        amount: 250000,
        currency: "usd",
        daysUntilDue: 14,
        description: "Enterprise launch invoice",
        operationId: "invoice-op-1",
        organizationId: "org-1",
      });
      return {
        data: {
          invoice: {
            amount_due: 250000,
            amount_paid: 0,
            created_at: "2026-09-28T00:00:00Z",
            currency: "usd",
            hosted_url: "https://billing.example/inv",
            id: "inv_1",
            number: "HEX-1",
            status: "open",
          },
        },
        error: null,
      };
    },
  });

  assert.deepEqual(result, { ok: true });
  assert.equal(audited.length, 1);
  assert.deepEqual(audited[0], {
    action: "customer.invoice.raise",
    actor: ACTOR.email,
    after: {
      amount: 250000,
      currency: "usd",
      daysUntilDue: 14,
      description: "Enterprise launch invoice",
      invoiceId: "inv_1",
    },
    resource: "org-1",
  });
});
