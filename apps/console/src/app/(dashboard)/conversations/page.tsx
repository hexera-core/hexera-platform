import Link from "next/link";
import { NavLink } from "@/app/_components/nav-link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { When } from "@/app/_components/when-time";
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
          <table className="table table--rows">
            <thead>
              <tr>
                <th className="col-main">Conversation</th>
                <th className="col-num col-opt">Messages</th>
                <th className="col-when">Started</th>
                <th className="col-icon"><span className="sr-only">Run</span></th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((conversation) => (
                <tr key={conversation.id}>
                  <td className="col-main">
                    <Link className="row-link" href={`/conversations/${conversation.id}`}>
                      {conversation.task_label ?? <span className="cell-faint">Untitled</span>}
                    </Link>
                  </td>
                  <td className="col-num col-opt">{conversation.message_count}</td>
                  <td className="col-when">
                    <When iso={conversation.created_at} />
                  </td>
                  <td className="col-icon">
                    {conversation.job_id ? (
                      <NavLink
                        aria-label="Open the run"
                        className="row-action"
                        href={`/runs/${conversation.job_id}`}
                        title="Open the run"
                      >
                        <svg aria-hidden="true" fill="none" focusable="false" stroke="currentColor"
                          strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" viewBox="0 0 24 24">
                          <path d="M7 17 17 7M9 7h8v8" />
                        </svg>
                      </NavLink>
                    ) : null}
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
