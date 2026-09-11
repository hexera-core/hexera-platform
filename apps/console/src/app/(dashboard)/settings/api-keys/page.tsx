import Link from "next/link";
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

type KeyPage = { items: Key[]; next_cursor: string | null };

export default async function ApiKeysPage({
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
  // PAGED like every other collection. Nothing bounds how many keys an organisation may mint,
  // so this page asks for a window rather than an account's entire history of them.
  const keys = await consoleFetch<KeyPage>(
    `api-keys${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    ownerId,
  );

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
        <>
          <ApiKeysPanel keys={keys.items} />
          {keys.next_cursor ? (
            <Link
              className="btn"
              href={`/settings/api-keys?cursor=${encodeURIComponent(keys.next_cursor)}`}
            >
              Older keys
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
