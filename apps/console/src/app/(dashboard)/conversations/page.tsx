import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Conversation = {
  id: string;
  task_label: string | null;
  job_id: string | null;
  message_count: number;
  created_at: string | null;
};

type ConversationPage = { items: Conversation[]; next_cursor: string | null };

export default async function ConversationsPage({
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
  const page = await consoleFetch<ConversationPage>(
    `chat${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
    ownerId,
  );

  return (
    <div className="page">
      <div className="page__head">
        <h1 className="page__title">Conversations</h1>
      </div>

      {page === null ? (
        <p className="empty">Could not reach the API just now. Reload to try again.</p>
      ) : page.items.length === 0 ? (
        <p className="empty">No conversations yet. Every run starts with one.</p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Study</th>
                <th>Messages</th>
                <th>Started</th>
                <th>Run</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((conversation) => (
                <tr key={conversation.id}>
                  <td>
                    <Link href={`/conversations/${conversation.id}`}>
                      {conversation.task_label ?? "Untitled study"}
                    </Link>
                  </td>
                  <td>{conversation.message_count}</td>
                  <td>
                    {conversation.created_at
                      ? new Date(conversation.created_at).toLocaleString()
                      : "—"}
                  </td>
                  <td>
                    {conversation.job_id ? (
                      <Link href={`/runs/${conversation.job_id}`}>Open</Link>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.next_cursor ? (
            <Link
              className="btn"
              href={`/conversations?cursor=${encodeURIComponent(page.next_cursor)}`}
            >
              Older conversations
            </Link>
          ) : null}
        </>
      )}
    </div>
  );
}
