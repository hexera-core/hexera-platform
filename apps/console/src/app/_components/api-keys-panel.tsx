"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { mintFailureMessage, revokeOutcomeMessage } from "@/app/_components/api-key-messages";

type Key = {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export function ApiKeysPanel({ keys }: { keys: Key[] }) {
  const router = useRouter();
  // THE MINTED SECRET LIVES HERE AND NOWHERE ELSE. It is in the POST response and in no row, no
  // list and no log; a lost key is replaced, never looked up. Holding it in component state is
  // what makes "shown once" literally true.
  const [minted, setMinted] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(formData: FormData) {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/api-keys", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: String(formData.get("name") ?? "") }),
      });
      if (!response.ok) {
        setError(mintFailureMessage(response.status));
        return;
      }
      const body = (await response.json()) as { presented: string };
      setMinted(body.presented);
      router.refresh();
    } catch {
      setError("Could not reach the API. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/v1/api-keys/${id}`, { method: "DELETE" });
      // `revoked` only exists on an ok body -- a 403 or 500 body has no such field, and treating
      // it as false-by-absence is exactly right: neither carries a revocation either.
      const revoked = response.ok && ((await response.json()) as { revoked: boolean }).revoked;
      const message = revokeOutcomeMessage(response.status, revoked);
      if (message) {
        setError(message);
      }
      router.refresh();
    } catch {
      setError("Could not reach the API. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form action={create} className="card" style={{ display: "grid", gap: ".7rem" }}>
        <label className="auth__field">
          <span className="label">Key name</span>
          <input className="auth__input" name="name" placeholder="ci" type="text" />
        </label>
        <button className="btn btn--gold" disabled={busy} type="submit">
          Mint a key
        </button>
      </form>

      {minted ? (
        <div className="card" role="status">
          <p className="label">Copy this now — it is not shown again</p>
          <code style={{ wordBreak: "break-all" }}>{minted}</code>
        </div>
      ) : null}

      {error ? (
        <div className="auth__error" role="status">
          {error}
        </div>
      ) : null}

      {keys.length === 0 ? (
        <p className="empty">No keys yet. A key lets a script submit runs as you.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Created</th>
              <th>Last used</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>{key.name || "—"}</td>
                <td>
                  <code>{key.key_prefix}</code>
                </td>
                <td>{key.created_at ? new Date(key.created_at).toLocaleDateString() : "—"}</td>
                <td>
                  {key.last_used_at ? new Date(key.last_used_at).toLocaleDateString() : "never"}
                </td>
                <td>
                  {key.revoked_at ? (
                    <span className="status status--fail">revoked</span>
                  ) : (
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => revoke(key.id)}
                      type="button"
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
