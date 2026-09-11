import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type Message = { role: string; content: string };

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
        <h1 className="page__title">Conversation</h1>
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
        <p className="empty">Could not load this conversation. Reload to try again.</p>
      ) : history.messages.length === 0 ? (
        <p className="empty">This conversation has no messages.</p>
      ) : (
        <div style={{ display: "grid", gap: ".9rem" }}>
          {history.messages.map((message, index) => (
            <div className="card" key={`${message.role}-${index}`}>
              <p className="label">{message.role}</p>
              <p style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
