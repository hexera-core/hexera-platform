import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { formatSigned } from "@/app/_components/ledger";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Entry = {
  id: string;
  entry_type: string;
  amount: number;
  reason: string;
  created_at: string | null;
};

type LedgerPage = { items: Entry[]; next_cursor: string | null };

export default async function UsagePage({
  searchParams,
}: {
  searchParams?: Promise<{ cursor?: string | string[] }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const params = await searchParams;
  const cursor = Array.isArray(params?.cursor) ? params.cursor[0] : params?.cursor;

  // Two independent soft reads: a failing ledger must not blank the balance, and vice versa.
  const [credits, ledger] = await Promise.all([
    consoleFetch<{ balance: number; unit: string }>("credits", ownerId),
    consoleFetch<LedgerPage>(
      `credits/history${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
      ownerId,
    ),
  ]);

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Capacity</p>
          <h1 className="page__title">Credits and usage</h1>
          <p className="page__intro">
            Credit movements across signup grants, plan allowances, one-off purchases and mesh runs.
          </p>
        </div>
        <Link className="btn" href="/settings/billing">
          Billing
        </Link>
      </div>

      <div className="summary-grid">
        <div className="metric-card">
          <p className="label">Balance</p>
          <b>{credits ? credits.balance.toLocaleString() : "--"}</b>
          <span>{credits ? credits.unit : "unavailable"} remaining</span>
        </div>
        <div className="metric-card">
          <p className="label">Charge rule</p>
          <b>Base + minutes</b>
          <span>failed runs cost nothing; successful runs debit the ledger</span>
        </div>
      </div>

      {ledger === null ? (
        <p className="empty card">Could not reach the API just now. Reload to try again.</p>
      ) : ledger.items.length === 0 ? (
        <p className="empty card">
          No movements yet. Your signup grant appears here the moment it lands.
        </p>
      ) : (
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">Credit ledger</p>
              <p className="page__sub">Append-only balance movements for this organisation.</p>
            </div>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Type</th>
                <th>Amount</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {ledger.items.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    {entry.created_at ? new Date(entry.created_at).toLocaleString() : "—"}
                  </td>
                  <td>
                    <span className="label">{entry.entry_type}</span>
                  </td>
                  <td>{formatSigned(entry.amount)}</td>
                  <td>{entry.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {ledger.next_cursor ? (
            <div className="panel__body">
              <Link
                className="btn"
                href={`/usage?cursor=${encodeURIComponent(ledger.next_cursor)}`}
              >
                Older movements
              </Link>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}
