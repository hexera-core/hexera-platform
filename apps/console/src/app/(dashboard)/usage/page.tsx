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
        <h1 className="page__title">Usage</h1>
      </div>

      <div className="card">
        <p className="label">Balance</p>
        <p style={{ fontSize: "1.8rem", fontFamily: "var(--mono)" }}>
          {credits ? `${credits.balance} ${credits.unit}` : "unavailable"}
        </p>
        <p className="page__sub">
          What a credit buys is not decided yet, and nothing spends them in this release.
        </p>
      </div>

      {ledger === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : ledger.items.length === 0 ? (
        <p className="empty">
          No movements yet. Your signup grant appears here the moment it lands.
        </p>
      ) : (
        <>
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
            <Link
              className="btn"
              href={`/usage?cursor=${encodeURIComponent(ledger.next_cursor)}`}
            >
              Older movements
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
