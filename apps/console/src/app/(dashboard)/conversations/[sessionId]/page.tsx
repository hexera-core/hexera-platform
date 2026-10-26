import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Message = { role: string; content: string };

/** Who said it, in the words the workbench uses: "You" and "Hexera", never the API's role names. */
function speaker(role: string): string {
  if (role === "user") return "You";
  if (role === "assistant") return "Hexera";
  return role;
}

type History = {
  session_id: string;
  messages: Message[];
  awaiting_confirmation: boolean;
  job_id: string | null;
};

export default async function ConversationPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const { sessionId } = await params;
  const history = await consoleFetch<History>(`chat/history/${sessionId}`, ownerId);

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Conversation</p>
          <h1 className="page__title">What was said</h1>
          <p className="page__intro">
            Your messages and the replies from Hexera, in order, up to the run they started.
          </p>
        </div>
        {history?.job_id ? (
          <Link className="btn" href={`/runs/${history.job_id}`}>
            Open the run
          </Link>
        ) : null}
      </div>

      {history === null ? (
        // A 404 for somebody else's session and an unreachable API both land here. The route
        // already refuses another tenant's id indistinguishably from a missing one, so this page
        // must not describe which of the two happened either.
        <DashboardNotice
          action={{ href: "/conversations", label: "Back to conversations" }}
          body="Refresh the page to try again, or return to your conversation history."
          retryLabel="Try again"
          title="We couldn't load this conversation"
          tone="error"
        />
      ) : history.messages.length === 0 ? (
        <DashboardNotice
          action={{ href: "/conversations", label: "Back to conversations" }}
          body="There is no conversation history to show for this run."
          title="No messages recorded"
        />
      ) : (
        <section className="panel">
          <div className="panel__head">
            <div>
              <p className="label">Messages</p>
              <p className="page__sub">
                {history.awaiting_confirmation ? "Waiting for you to confirm" : "Full conversation"}
              </p>
            </div>
          </div>
          <div className="message-stack">
            {history.messages.map((message, index) => (
              <div className="message-card" key={`${message.role}-${index}`}>
                <p className="label">{speaker(message.role)}</p>
                <p style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
