import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { AccountForm } from "@/app/_components/account-form";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function AccountPage() {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!ownerId) {
    redirect("/sign-in");
  }

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Operator identity</p>
          <h1 className="page__title">Account</h1>
          <p className="page__intro">
            Keep the console identity current without changing the owner address that scopes runs,
            geometry and conversations.
          </p>
        </div>
      </div>

      {/* Decision 4 of the 09-10 signup design: owner_id IS the lowercased address, and every
            job, geometry and chat session is scoped on it. Changing it here would strand all of
            them, so the UI does not offer it. */}
      <div className="summary-grid">
        <div className="metric-card">
          <p className="label">Email</p>
          <b style={{ fontSize: "1rem", overflowWrap: "anywhere" }}>{ownerId}</b>
          <span>Your address identifies your account and cannot be changed here.</span>
        </div>
      </div>

      <section className="panel">
        <div className="panel__head">
          <div>
            <p className="label">Profile</p>
            <p className="page__sub">Display name and password controls.</p>
          </div>
        </div>
        <div className="panel__body">
          <AccountForm name={session?.user?.name ?? ""} />
        </div>
      </section>
    </div>
  );
}
