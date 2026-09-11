import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ApiKeysPanel } from "@/app/_components/api-keys-panel";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Key = {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export default async function ApiKeysPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const keys = await consoleFetch<{ items: Key[] }>("api-keys", ownerId);

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">API keys</h1>
      </div>
      <p className="page__sub">
        A key acts as you against the API. The secret half is shown once, when it is minted, and
        is not recoverable — a lost key is replaced, never looked up.
      </p>
      {keys === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : (
        <ApiKeysPanel keys={keys.items} />
      )}
    </div>
  );
}
