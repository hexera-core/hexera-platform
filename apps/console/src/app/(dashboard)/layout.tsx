import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { DashboardStyles } from "@/app/_components/legacy-styles";
import { Sidebar } from "@/app/_components/sidebar";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

type CreditBalance = { balance: number; unit: string };

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  const ownerId = ownerIdFromSession(session);
  if (!session || !ownerId) {
    redirect("/sign-in");
  }
  // DECISION 4. An unverified address does not reach the product. /verify-email lives in the
  // (auth) group, which has no such gate, so there is no redirect loop.
  if (!session.emailVerified) {
    redirect("/verify-email");
  }

  const credits = await consoleFetch<CreditBalance>("credits", ownerId);

  return (
    <>
      <DashboardStyles />
      <div className="shell">
        <Sidebar
          credits={credits ? `${credits.balance}` : "--"}
          email={ownerId}
          signOut={<SignOutButton />}
        />
        <main className="main">{children}</main>
      </div>
    </>
  );
}
