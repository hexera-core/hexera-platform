import { notFound } from "next/navigation";

import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import { isLaunchAdminSurfaceEnabled } from "@/app/_components/sections";
import {
  compactDate,
  readCustomers,
  signedNumber,
  sortCustomers,
  type AdminOrganization,
} from "@/lib/admin/read";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function nextHref(key: string, sort: string, direction: string): string {
  const nextDirection = sort === key && direction !== "asc" ? "asc" : "desc";
  return `/customers?sort=${encodeURIComponent(key)}&direction=${nextDirection}`;
}

function SortHeader({
  children,
  direction,
  name,
  sort,
}: {
  children: React.ReactNode;
  direction: string;
  name: string;
  sort: string;
}) {
  return (
    <th>
      <a href={nextHref(name, sort, direction)}>{children}</a>
    </th>
  );
}

export default async function CustomersPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  if (!isLaunchAdminSurfaceEnabled()) notFound();

  const params = await searchParams;
  const sort = typeof params.sort === "string" ? params.sort : "name";
  const direction = typeof params.direction === "string" ? params.direction : "asc";
  const customers = await readCustomers();
  const rows = sortCustomers(customers.data?.organizations ?? [], sort, direction);

  const totalBalance = rows.reduce((sum, org) => sum + org.balance, 0);
  const activeRuns = rows.reduce((sum, org) => sum + org.active_runs, 0);

  return (
    <>
      <h1>Customers</h1>
      <p className="admin-lede">
        Every organisation, its plan, balance and current run load. Open a row for members, ledger,
        recent runs and operator controls.
      </p>

      <Panel title="Overview" heading="derived from organisations, jobs and credit_ledger">
        <StatRow
          stats={[
            { label: "Organisations", value: rows.length.toLocaleString("en-US") },
            { label: "Active runs", value: activeRuns.toLocaleString("en-US") },
            { label: "Net balance", value: signedNumber(totalBalance) },
          ]}
        />
      </Panel>

      <Panel title="Organisations" heading="click a heading to sort">
        {customers.error ? (
          <Alert>{customers.error}</Alert>
        ) : rows.length ? (
          <table className="admin-table">
            <thead>
              <tr>
                <SortHeader name="name" sort={sort} direction={direction}>Organisation</SortHeader>
                <SortHeader name="plan" sort={sort} direction={direction}>Plan</SortHeader>
                <SortHeader name="status" sort={sort} direction={direction}>Status</SortHeader>
                <SortHeader name="balance" sort={sort} direction={direction}>Balance</SortHeader>
                <SortHeader name="runs" sort={sort} direction={direction}>Runs</SortHeader>
                <SortHeader name="active" sort={sort} direction={direction}>Active</SortHeader>
                <SortHeader name="last-run" sort={sort} direction={direction}>Last run</SortHeader>
                <SortHeader name="created" sort={sort} direction={direction}>Created</SortHeader>
              </tr>
            </thead>
            <tbody>
              {rows.map((org: AdminOrganization) => (
                <tr key={org.id}>
                  <td><a href={`/customers/${org.id}`}>{org.name}</a></td>
                  <td>{org.plan || "free"}</td>
                  <td>{org.status || (org.has_billing_account ? "no subscription" : "free")}</td>
                  <td>{signedNumber(org.balance)}</td>
                  <td>{org.run_count.toLocaleString("en-US")}</td>
                  <td>{org.active_runs.toLocaleString("en-US")}</td>
                  <td>{compactDate(org.last_run_at)}</td>
                  <td>{compactDate(org.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState note="No organisations exist in this deployment yet." />
        )}
      </Panel>
    </>
  );
}
