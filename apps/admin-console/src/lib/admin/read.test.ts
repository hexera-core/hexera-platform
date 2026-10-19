import assert from "node:assert/strict";
import test from "node:test";

import { filterCustomers, type AdminOrganization } from "./read";

const BASE: AdminOrganization = {
  active_runs: 0,
  balance: 0,
  created_at: null,
  current_period_end: null,
  has_billing_account: false,
  id: "org-1",
  included_credits: 0,
  last_run_at: null,
  name: "Acme Medical",
  plan: "",
  run_count: 0,
  slug: "acme",
  status: "",
  stripe_customer_id: "",
};

test("customer search matches name slug plan and provider customer id", () => {
  const rows = [
    BASE,
    { ...BASE, id: "org-2", name: "Borealis", plan: "enterprise", slug: "borealis" },
    { ...BASE, id: "org-3", name: "Continuum", slug: "continuum", stripe_customer_id: "cus_123" },
  ];

  assert.deepEqual(filterCustomers(rows, " acme ").map((row) => row.id), ["org-1"]);
  assert.deepEqual(filterCustomers(rows, "enterprise").map((row) => row.id), ["org-2"]);
  assert.deepEqual(filterCustomers(rows, "CUS_123").map((row) => row.id), ["org-3"]);
});
