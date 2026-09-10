import assert from "node:assert/strict";
import test from "node:test";

import { readBillingAccount, readBudgets, readSpendByService, spendQuery } from "./costs";

test("reads which billing account pays for this project", async () => {
  const client = {
    getProjectBillingInfo: async () =>
      [{ billingAccountName: "billingAccounts/01ABCD-234567-89EFGH", billingEnabled: true }] as never,
  } as never;

  const account = await readBillingAccount(client, "hexera-dev");
  assert.equal(account.name, "billingAccounts/01ABCD-234567-89EFGH");
  assert.equal(account.enabled, true);
});

test("a project with no billing account is a state, not a crash", async () => {
  const client = { getProjectBillingInfo: async () => [{ billingEnabled: false }] as never } as never;

  const account = await readBillingAccount(client, "hexera-dev");
  assert.equal(account.name, null);
  assert.equal(account.enabled, false);
});

test("reads budgets, their amounts and their alert thresholds", async () => {
  const client = {
    listBudgets: async () =>
      [
        [
          {
            amount: { specifiedAmount: { currencyCode: "USD", nanos: 500000000, units: "400" } },
            displayName: "hexera-dev monthly",
            name: "billingAccounts/x/budgets/1",
            thresholdRules: [{ thresholdPercent: 0.5 }, { thresholdPercent: 0.9 }],
          },
        ],
        null,
        {},
      ] as never,
  } as never;

  const budgets = await readBudgets(client, "billingAccounts/x");
  assert.equal(budgets.length, 1);
  assert.equal(budgets[0].displayName, "hexera-dev monthly");
  assert.equal(budgets[0].currency, "USD");
  // units and nanos together are the amount: 400 + 0.5
  assert.equal(budgets[0].amount, 400.5);
  assert.deepEqual(budgets[0].thresholdPercents, [50, 90]);
});

test("a budget with no explicit amount reports none rather than zero", async () => {
  // lastPeriodAmount budgets track the previous period and carry no figure of their own. Rendering
  // that as $0 would say the budget is zero, which is the opposite of what it means.
  const client = {
    listBudgets: async () =>
      [[{ amount: { lastPeriodAmount: {} }, displayName: "rolling", name: "b" }], null, {}] as never,
  } as never;

  const budgets = await readBudgets(client, "billingAccounts/x");
  assert.equal(budgets[0].amount, null);
  assert.equal(budgets[0].basis, "last period");
});

test("the spend query reads the export table over a bounded window", () => {
  const query = spendQuery("hexera-billing.billing_export.gcp_billing_export_v1_ABC", 30);

  assert.match(query.query, /FROM `hexera-billing\.billing_export\.gcp_billing_export_v1_ABC`/);
  assert.match(query.query, /GROUP BY service, currency/);
  assert.match(query.query, /_PARTITIONTIME|usage_start_time/);
  assert.equal(query.params.days, 30);
});

test("the export table name is refused unless it is a plain BigQuery reference", () => {
  // This value is interpolated into SQL - BigQuery does not parameterise table names - so anything
  // that is not project.dataset.table is refused rather than escaped.
  assert.throws(() => spendQuery("a.b.c`; DROP TABLE x; --", 30), /table reference/i);
  assert.throws(() => spendQuery("not-three-parts", 30), /table reference/i);
});

test("sums spend by service, largest first", async () => {
  const bq = {
    query: async () =>
      [
        [
          { cost: 12.5, currency: "USD", service: "Compute Engine" },
          { cost: 40.25, currency: "USD", service: "Cloud Run" },
        ],
      ] as never,
  } as never;

  const rows = await readSpendByService(bq, "p.d.t", 30);
  assert.deepEqual(
    rows.map((row) => row.service),
    ["Cloud Run", "Compute Engine"],
  );
  assert.equal(rows[0].amount, 40.25);
});

test("an export that returns nothing is an empty list", async () => {
  const bq = { query: async () => [[]] as never } as never;
  assert.deepEqual(await readSpendByService(bq, "p.d.t", 30), []);
});
