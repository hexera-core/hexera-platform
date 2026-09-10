import { Alert, EmptyState, FieldList, Panel, StatRow } from "@/app/_components/panel";
import { getBillingReader, getBudgetReader, getSpendReader } from "@/lib/gcp/clients";
import { readAdminTargets } from "@/lib/gcp/config";
import {
  readBillingAccount,
  readBudgets,
  readSpendByService,
  type BillingAccount,
  type BudgetSummary,
  type SpendRow,
} from "@/lib/gcp/costs";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const WINDOW_DAYS = 30;

function money(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat("en-US", { currency: currency || "USD", style: "currency" }).format(
      amount,
    );
  } catch {
    return `${amount.toFixed(2)} ${currency}`.trim();
  }
}

export default async function CostsPage() {
  const targets = readAdminTargets(process.env);

  // EACH READ FAILS ON ITS OWN. These three sit behind different grants, and one of them -
  // roles/billing.viewer on the billing ACCOUNT - cannot be granted by the deploy at all, because a
  // deploy identity has no authority on a billing account. So a missing grant must degrade the
  // panel that needs it, not the page: a Costs page that renders nothing but a stack trace is
  // strictly less useful than one that renders the budget it could read and names the grant it
  // could not.
  let account: BillingAccount = { enabled: false, name: null };
  let accountError: string | null = null;
  try {
    account = await readBillingAccount(getBillingReader(), targets.projectId);
  } catch (error) {
    accountError = error instanceof Error ? error.message : String(error);
  }

  let budgets: BudgetSummary[] = [];
  let budgetError: string | null = null;
  if (account.name) {
    try {
      budgets = await readBudgets(getBudgetReader(), account.name);
    } catch (error) {
      budgetError = error instanceof Error ? error.message : String(error);
    }
  }

  // The breakdown exists only where an export does. Its absence is reported as such rather than
  // caught and rendered as zero spend.
  let spend: SpendRow[] = [];
  let spendError: string | null = null;
  if (targets.billingExportTable) {
    try {
      spend = await readSpendByService(
        getSpendReader(targets.projectId),
        targets.billingExportTable,
        WINDOW_DAYS,
      );
    } catch (error) {
      spendError = error instanceof Error ? error.message : String(error);
    }
  }

  const total = spend.reduce((sum, row) => sum + row.amount, 0);
  const currency = spend[0]?.currency ?? "USD";

  return (
    <>
      <h1>Costs</h1>

      <Panel title="Billing account">
        {/* A failed read is NOT the same statement as "no billing account". Rendering both would
            claim something this page does not know. */}
        {accountError ? (
          <Alert>
            The billing account could not be read: {accountError}. This needs
            <code> roles/billing.viewer</code> on the project, which
            <code> create-admin-service.sh</code> grants. Whether this project has a billing
            account is unknown until it can be read — it is not being reported as absent.
          </Alert>
        ) : account.enabled ? (
          <FieldList
            fields={[
              { label: "Project", value: targets.projectId },
              { label: "Account", value: account.name ?? "—" },
              { label: "Billing", value: "enabled" },
            ]}
          />
        ) : (
          <EmptyState note={`${targets.projectId} has no billing account attached, so there is nothing to report against. Everything it runs is on a free tier or is not running.`} />
        )}
      </Panel>

      <Panel heading={budgetError || accountError ? undefined : `${budgets.length} configured`} title="Budgets">
        {accountError ? (
          <EmptyState note="Budgets were not read, because the billing account they hang off could not be read. See above." />
        ) : budgetError ? (
          <Alert>
            Budgets could not be read: {budgetError}. Budgets live on the billing ACCOUNT, not on
            this project, and a deploy identity has no authority there — so this one grant is made
            by hand, once. See §10 of <code>docs/deployment/admin-console-access.md</code>.
          </Alert>
        ) : budgets.length === 0 ? (
          <EmptyState note="No budget is set on this billing account. A budget is what turns spend into an alert; without one, an unexpected bill is discovered at the end of the month." />
        ) : (
          budgets.map((budget) => (
            <div className="admin-budget" key={budget.displayName}>
              <StatRow
                stats={[
                  { label: "Budget", value: budget.displayName },
                  {
                    label: "Amount",
                    value:
                      budget.amount === null
                        ? `tracks ${budget.basis}`
                        : money(budget.amount, budget.currency ?? "USD"),
                  },
                  {
                    label: "Alerts at",
                    value: budget.thresholdPercents.length
                      ? budget.thresholdPercents.map((percent) => `${percent}%`).join(", ")
                      : "no thresholds",
                  },
                ]}
              />
            </div>
          ))
        )}
      </Panel>

      <Panel heading={`last ${WINDOW_DAYS} days`} title="Spend by service">
        {spendError ? (
          <Alert>
            The billing export at <code>{targets.billingExportTable}</code> could not be read:{" "}
            {spendError}
          </Alert>
        ) : null}

        {!targets.billingExportTable ? (
          <EmptyState
            note={
              "No per-service breakdown, because this deployment names no BigQuery billing export. " +
              "The Cloud Billing API returns budgets and account state; it does not return spend by " +
              "service over time — only a billing export does. That export accumulates from the day " +
              "it is enabled and CANNOT be backfilled, so enabling it is worth doing now whether or " +
              "not this page is read today. Enable it on the billing account, then set " +
              "BILLING_EXPORT_TABLE to project.dataset.table."
            }
          />
        ) : spend.length === 0 && !spendError ? (
          <EmptyState note={`The export at ${targets.billingExportTable} returned no rows for the last ${WINDOW_DAYS} days. An export enabled recently has no history yet.`} />
        ) : spend.length > 0 ? (
          <>
            <StatRow stats={[{ label: `Total, ${WINDOW_DAYS}d`, value: money(total, currency) }]} />
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Cost</th>
                  <th>Share</th>
                </tr>
              </thead>
              <tbody>
                {spend.map((row) => (
                  <tr key={row.service}>
                    <td>{row.service}</td>
                    <td>{money(row.amount, row.currency)}</td>
                    <td>{total > 0 ? `${Math.round((row.amount / total) * 100)}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : null}
      </Panel>

      <Panel title="What this page does not answer">
        <EmptyState note="This is 'roughly what, roughly where'. It is not chargeback: costs are not attributed to a customer, a job or an environment, because nothing in the platform tags spend that way yet. The Fleet page's ceiling is the closest thing to a cost control the console offers." />
      </Panel>
    </>
  );
}
