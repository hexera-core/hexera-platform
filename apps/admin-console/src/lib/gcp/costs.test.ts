import assert from "node:assert/strict";
import test from "node:test";

import {
  isExportNotYetWritten,
  resolveExportTable,
  readBillingAccount,
  readBudgets,
  readSpendByService,
  spendQuery,
} from "./costs";

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
            budgetFilter: { projects: ["projects/224734058693"] },
            displayName: "hexera-dev monthly",
            name: "billingAccounts/x/budgets/1",
            thresholdRules: [{ thresholdPercent: 0.5 }, { thresholdPercent: 0.9 }],
          },
        ],
        null,
        {},
      ] as never,
  } as never;

  const budgets = await readBudgets(client, "billingAccounts/x", "224734058693");
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

  const budgets = await readBudgets(client, "billingAccounts/x", "224734058693");
  assert.equal(budgets[0].amount, null);
  assert.equal(budgets[0].basis, "last period");
});

test("the query is scoped to this project, because one export carries several", () => {
  // hexera-dev and hexera-prod share a billing account, so one export table holds both. Without
  // this filter dev's Costs page would show dev + prod, and prod is the larger of the two.
  const query = spendQuery("acct.billing_export.gcp_billing_export_v1_ABC", 30, "hexera-dev");
  assert.match(query.query, /WHERE project\.id = @projectId/);
  assert.equal(query.params.projectId, "hexera-dev");
});

test("the spend query reads the export table over a bounded window", () => {
  const query = spendQuery("hexera-billing.billing_export.gcp_billing_export_v1_ABC", 30, "hexera-dev");

  assert.match(query.query, /FROM `hexera-billing\.billing_export\.gcp_billing_export_v1_ABC`/);
  assert.match(query.query, /GROUP BY service, currency/);
  assert.match(query.query, /_PARTITIONTIME|usage_start_time/);
  assert.equal(query.params.days, 30);
  assert.equal(query.params.projectId, "hexera-dev");
});

test("the export table name is refused unless it is a plain BigQuery reference", () => {
  // This value is interpolated into SQL - BigQuery does not parameterise table names - so anything
  // that is not project.dataset.table is refused rather than escaped.
  assert.throws(() => spendQuery("a.b.c`; DROP TABLE x; --", 30, "p"), /table reference/i);
  assert.throws(() => spendQuery("not-three-parts", 30, "p"), /table reference/i);
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

  const rows = await readSpendByService(bq, "p.d.t", 30, "hexera-dev");
  assert.deepEqual(
    rows.map((row) => row.service),
    ["Cloud Run", "Compute Engine"],
  );
  assert.equal(rows[0].amount, 40.25);
});

test("an export that returns nothing is an empty list", async () => {
  const bq = { query: async () => [[]] as never } as never;
  assert.deepEqual(await readSpendByService(bq, "p.d.t", 30, "hexera-dev"), []);
});

test("an export with no table yet is waiting, not broken", () => {
  // The export writes its first table hours after being enabled. "Check back tomorrow" and "your
  // configuration is wrong" are different instructions.
  assert.equal(
    isExportNotYetWritten(new Error("Not found: Table hexera-dev:billing_export.gcp_billing_export_v1_X")),
    true,
  );
  assert.equal(isExportNotYetWritten(new Error("Access Denied: Table ...")), false);
  assert.equal(isExportNotYetWritten(new Error("Syntax error")), false);
});

test("shows only budgets that apply to this project", () => {
  // Budgets are listed per BILLING ACCOUNT and hexera-dev and hexera-prod share one, so an
  // unfiltered list puts prod's budget on dev's page.
  const client = {
    listBudgets: async () =>
      [
        [
          { budgetFilter: { projects: ["projects/224734058693"] }, displayName: "dev", name: "b1" },
          { budgetFilter: { projects: ["projects/688073002171"] }, displayName: "prod", name: "b2" },
          { displayName: "whole account", name: "b3" },
        ],
        null,
        {},
      ] as never,
  } as never;

  return readBudgets(client, "billingAccounts/x", "224734058693").then((budgets) => {
    assert.deepEqual(
      budgets.map((b) => b.displayName),
      ["dev", "whole account"],
      "an account-wide budget applies here too; another project's does not",
    );
    assert.equal(budgets[0].scopedToProject, true);
    assert.equal(budgets[1].scopedToProject, false);
  });
});

test("with no project number, only account-wide budgets are claimed", () => {
  const client = {
    listBudgets: async () =>
      [[{ budgetFilter: { projects: ["projects/1"] }, displayName: "other", name: "b" }], null, {}] as never,
  } as never;

  return readBudgets(client, "billingAccounts/x", null).then((budgets) => {
    assert.deepEqual(budgets, []);
  });
});

test("an exact table reference is used as given", async () => {
  const bq = { query: async () => { throw new Error("must not query"); } } as never;
  assert.equal(
    await resolveExportTable(bq, "hexera-prod.billing_export.gcp_billing_export_v1_01EFBB_8FF368_9E335F"),
    "hexera-prod.billing_export.gcp_billing_export_v1_01EFBB_8FF368_9E335F",
  );
});

test("a dataset reference discovers the standard export table", async () => {
  // The table name is derived from the billing account id. Deriving it in config and being wrong
  // fails exactly like a disabled export - "Not found: Table" - so the mistake is indistinguishable
  // from the state it is waiting on. Finding it removes the guess.
  let sql = "";
  const bq = {
    query: async (req: { query: string }) => {
      sql = req.query;
      return [[{ table_name: "gcp_billing_export_v1_01EFBB_8FF368_9E335F" }]] as never;
    },
  } as never;

  assert.equal(
    await resolveExportTable(bq, "hexera-prod.billing_export"),
    "hexera-prod.billing_export.gcp_billing_export_v1_01EFBB_8FF368_9E335F",
  );
  assert.match(sql, /INFORMATION_SCHEMA\.TABLES/);
});

test("the detailed-usage export is not mistaken for the standard one", async () => {
  // Both land in the same dataset and both begin gcp_billing_export. Only the standard table has
  // the shape this query reads.
  const bq = { query: async (req: { query: string }) => { assert.match(req.query, /NOT LIKE '%_resource_v1_%'/); return [[]] as never; } } as never;
  await assert.rejects(() => resolveExportTable(bq, "hexera-prod.billing_export"), /Not found: Table/);
});

test("an empty dataset reads as the waiting state, not a broken one", async () => {
  const bq = { query: async () => [[]] as never } as never;
  await assert.rejects(
    () => resolveExportTable(bq, "hexera-prod.billing_export"),
    (error: Error) => isExportNotYetWritten(error),
  );
});

test("anything that is not a dataset or table reference is refused", async () => {
  const bq = { query: async () => [[]] as never } as never;
  await assert.rejects(() => resolveExportTable(bq, "a.b`; DROP TABLE x; --"), /refused/i);
  await assert.rejects(() => resolveExportTable(bq, "onepart"), /refused/i);
});
