import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
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
        <div>
          <p className="page__eyebrow">Conversations</p>
          <h1 className="page__title">Your conversations</h1>
          <p className="page__intro">
            What you and Hexera said before each run: your answers and what you confirmed.
          </p>
        </div>
      </div>

      {page === null ? (
        <DashboardNotice
          body="Refresh the page to try again. Your conversation history is not affected."
          retryLabel="Try again"
          title="We couldn't load your conversations"
          tone="error"
        />
      ) : page.items.length === 0 ? (
        <DashboardNotice
          action={{ href: "/runs/new", label: "Start a run" }}
          body="Every mesh run begins with a short conversation about your geometry and solver."
          title="No conversations yet"
        />
      ) : (
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">All conversations</p>
              <p className="page__sub">Each conversation, its length and the run it started.</p>
            </div>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>Conversation</th>
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
                      {conversation.task_label ?? "Untitled"}
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
            <div className="panel__body">
              <Link
                className="btn"
                href={`/conversations?cursor=${encodeURIComponent(page.next_cursor)}`}
              >
                Older conversations
              </Link>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}
