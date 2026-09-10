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
  thresholdPercents: readonly number[];
};

export type SpendRow = { amount: number; currency: string; service: string };

export type BillingReader = Pick<CloudBillingClient, "getProjectBillingInfo">;
export type BudgetReader = Pick<BudgetServiceClient, "listBudgets">;
export type SpendReader = Pick<BigQuery, "query">;

// project.dataset.table, and nothing else. BigQuery does not parameterise table names, so this
// value is interpolated into SQL - which makes validating it the only thing standing between a
// configuration setting and an injected query.
const TABLE_REFERENCE = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$/;

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

export async function readBudgets(
  client: BudgetReader,
  billingAccount: string,
): Promise<BudgetSummary[]> {
  const [budgets] = await client.listBudgets({ parent: billingAccount });

  return (budgets ?? []).map((budget) => {
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
      thresholdPercents: (budget.thresholdRules ?? [])
        .map((rule) => Math.round((rule.thresholdPercent ?? 0) * 100))
        .sort((a, b) => a - b),
    };
  });
}

export function spendQuery(table: string, days: number): { params: { days: number }; query: string } {
  if (!TABLE_REFERENCE.test(table)) {
    throw new Error(
      `'${table}' is not a BigQuery table reference. BILLING_EXPORT_TABLE must be ` +
        `project.dataset.table - it is interpolated into SQL, which BigQuery cannot parameterise.`,
    );
  }

  return {
    params: { days },
    query: `
      SELECT
        service.description AS service,
        currency,
        SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS cost
      FROM \`${table}\`
      WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
      GROUP BY service, currency
      ORDER BY cost DESC
    `,
  };
}

export async function readSpendByService(
  bigquery: SpendReader,
  table: string,
  days: number,
): Promise<SpendRow[]> {
  const { params, query } = spendQuery(table, days);
  const [rows] = await bigquery.query({ params, query });

  return (rows as { cost?: number; currency?: string; service?: string }[])
    .map((row) => ({
      amount: toNumber(row.cost) ?? 0,
      currency: row.currency ?? "",
      service: row.service ?? "unattributed",
    }))
    .sort((a, b) => b.amount - a.amount);
}
