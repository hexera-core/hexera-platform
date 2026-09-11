import type { BigQuery } from "@google-cloud/bigquery";
import type { CloudBillingClient } from "@google-cloud/billing";
import type { BudgetServiceClient } from "@google-cloud/billing-budgets";

// WHAT THIS PLATFORM COSTS, and the limits of what can honestly be said about it.
//
// The Cloud Billing API answers "which account pays for this project" and the Budgets API answers
// "what did we say we would spend". NEITHER returns spend per service over time. That needs a
// BigQuery BILLING EXPORT, which is a project-level setting that accumulates only from the day it
// is switched on and CANNOT BE BACKFILLED.
//
// So the page is built to be correct either way. With an export configured it shows the breakdown;
// without one it shows the budget and says, naming the setting, that per-service history begins
// when the export is enabled. What it must never do is render an empty chart, because "we spent
// nothing" and "we are not recording this" look identical when both draw a flat line at zero.

export type BillingAccount = { enabled: boolean; name: string | null };

export type BudgetSummary = {
  amount: number | null;
  basis: string;
  currency: string | null;
  displayName: string;
  scopedToProject: boolean;
  thresholdPercents: readonly number[];
};

export type SpendRow = { amount: number; currency: string; service: string };

// A billing export starts writing from the day it is switched on, and the first table appears
// hours later. Between configuring BILLING_EXPORT_TABLE and that first write, BigQuery answers
// "Not found: Table ...", which is a WAITING state and not a broken one - telling them apart is
// the difference between "check your configuration" and "check back tomorrow".
export function isExportNotYetWritten(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /not found:\s*table/i.test(message);
}

export type BillingReader = Pick<CloudBillingClient, "getProjectBillingInfo">;
export type BudgetReader = Pick<BudgetServiceClient, "listBudgets">;
export type SpendReader = Pick<BigQuery, "query">;

// project.dataset.table, and nothing else. BigQuery does not parameterise table names, so this
// value is interpolated into SQL - which makes validating it the only thing standing between a
// configuration setting and an injected query.
const TABLE_REFERENCE = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$/;

// project.dataset - the form that lets the table be DISCOVERED rather than guessed.
const DATASET_REFERENCE = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+$/;

// The Standard usage cost export writes one table per billing account, named for the account with
// its hyphens turned to underscores. The other exports write their own prefixes into the same
// dataset - detailed usage cost is `gcp_billing_export_resource_v1_`, pricing is
// `cloud_pricing_export` - so the prefix is what tells them apart, and the `_resource_` one is
// excluded rather than matched by accident.
const STANDARD_EXPORT_PREFIX = "gcp_billing_export_v1_";

// WHY DISCOVERY EXISTS AT ALL. BILLING_EXPORT_TABLE was originally pinned to a table name derived
// from the billing account id, before any table existed to check it against. A derived name that
// turns out wrong fails the same way a disabled export does - "Not found: Table" - so the mistake
// would have looked exactly like the state it was waiting on, indefinitely. Naming the DATASET and
// finding the table removes the guess.
export async function resolveExportTable(bigquery: SpendReader, reference: string): Promise<string> {
  if (TABLE_REFERENCE.test(reference)) return reference;

  if (!DATASET_REFERENCE.test(reference)) {
    throw new Error(
      `'${reference}' is neither project.dataset.table nor project.dataset. BILLING_EXPORT_TABLE ` +
        `is interpolated into SQL, which BigQuery cannot parameterise, so anything else is refused.`,
    );
  }

  const [rows] = await bigquery.query({
    params: { prefix: `${STANDARD_EXPORT_PREFIX}%` },
    query: `
      SELECT table_name
      FROM \`${reference}.INFORMATION_SCHEMA.TABLES\`
      WHERE table_name LIKE @prefix AND table_name NOT LIKE '%_resource_v1_%'
      ORDER BY table_name
      LIMIT 1
    `,
  });

  const found = (rows as { table_name?: string }[])[0]?.table_name;
  if (!found) {
    throw new Error(
      `Not found: Table — the dataset ${reference} holds no ${STANDARD_EXPORT_PREFIX}* table yet. ` +
        `Either the Standard usage cost export is not enabled on the billing account, or it is and ` +
        `has not written its first table.`,
    );
  }

  return `${reference}.${found}`;
}

function toNumber(value: number | string | { toNumber(): number } | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "object") return value.toNumber();
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function readBillingAccount(
  client: BillingReader,
  projectId: string,
): Promise<BillingAccount> {
  const [info] = await client.getProjectBillingInfo({ name: `projects/${projectId}` });
  return { enabled: info.billingEnabled ?? false, name: info.billingAccountName || null };
}

// BUDGETS ARE LISTED PER BILLING ACCOUNT, NOT PER PROJECT - the same shape that made the spend
// query need a project filter. hexera-dev and hexera-prod share an account, so an unfiltered list
// shows dev's Costs page prod's budget alongside its own. A budget with no project filter applies
// to the whole account and is genuinely this project's business, so it is kept and marked.
export async function readBudgets(
  client: BudgetReader,
  billingAccount: string,
  projectNumber: string | null,
): Promise<BudgetSummary[]> {
  const [budgets] = await client.listBudgets({ parent: billingAccount });

  const applies = (scope: readonly string[]): boolean => {
    if (scope.length === 0) return true;
    if (!projectNumber) return false;
    return scope.includes(`projects/${projectNumber}`);
  };

  return (budgets ?? [])
    .filter((budget) => applies(budget.budgetFilter?.projects ?? []))
    .map((budget) => {
    const specified = budget.amount?.specifiedAmount;
    // A money value is units plus nanos, and nanos are a billionth. Reading only units silently
    // rounds every budget down to the dollar.
    const units = toNumber(specified?.units);
    const amount =
      units === null ? null : units + (toNumber(specified?.nanos) ?? 0) / 1_000_000_000;

    return {
      amount,
      // A lastPeriodAmount budget tracks the previous period and carries no figure of its own.
      // Rendering that as 0 would say the budget is zero, which is the opposite of what it means.
      basis: specified ? "specified" : "last period",
      currency: specified?.currencyCode || null,
      displayName: budget.displayName || budget.name || "unnamed budget",
      scopedToProject: (budget.budgetFilter?.projects ?? []).length > 0,
      thresholdPercents: (budget.thresholdRules ?? [])
        .map((rule) => Math.round((rule.thresholdPercent ?? 0) * 100))
        .sort((a, b) => a - b),
    };
  });
}

// THE PROJECT FILTER IS NOT OPTIONAL. A billing export is configured per BILLING ACCOUNT, and one
// account bills several projects - hexera-dev and hexera-prod share one. A single export therefore
// writes every project's usage into the same table, and a query without this filter would show
// dev's Costs page the sum of dev AND prod. That is not a rounding error; prod is the larger of the
// two, so the page would be mostly wrong and confidently so.
export function spendQuery(
  table: string,
  days: number,
  projectId: string,
): { params: { days: number; projectId: string }; query: string } {
  if (!TABLE_REFERENCE.test(table)) {
    throw new Error(
      `'${table}' is not a BigQuery table reference. BILLING_EXPORT_TABLE must be ` +
        `project.dataset.table - it is interpolated into SQL, which BigQuery cannot parameterise.`,
    );
  }

  return {
    params: { days, projectId },
    query: `
      SELECT
        service.description AS service,
        currency,
        SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS cost
      FROM \`${table}\`
      WHERE project.id = @projectId
        AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
      GROUP BY service, currency
      ORDER BY cost DESC
    `,
  };
}

export async function readSpendByService(
  bigquery: SpendReader,
  tableOrDataset: string,
  days: number,
  projectId: string,
): Promise<SpendRow[]> {
  const table = await resolveExportTable(bigquery, tableOrDataset);
  const { params, query } = spendQuery(table, days, projectId);
  const [rows] = await bigquery.query({ params, query });

  return (rows as { cost?: number; currency?: string; service?: string }[])
    .map((row) => ({
      amount: toNumber(row.cost) ?? 0,
      currency: row.currency ?? "",
      service: row.service ?? "unattributed",
    }))
    .sort((a, b) => b.amount - a.amount);
}
