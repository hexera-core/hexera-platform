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
        <h1 className="page__title">Account</h1>
      </div>

      <div className="card">
        <p className="label">Email</p>
        <p>{ownerId}</p>
        {/* Decision 4 of the 09-10 signup design: owner_id IS the lowercased address, and every
            job, geometry and chat session is scoped on it. Changing it here would strand all of
            them, so the UI does not offer it. */}
        <p className="page__sub">
          Your address identifies your account and cannot be changed here.
        </p>
      </div>

      <AccountForm name={session?.user?.name ?? ""} />
    </div>
  );
}
