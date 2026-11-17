import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { SignOutButton } from "@/app/_components/auth-buttons";
import { DashboardStyles } from "@/app/_components/legacy-styles";
import { RECENT_LIMIT, type RecentRun } from "@/app/_components/recent-runs";
import { Shell } from "@/app/_components/shell";
import { SIDEBAR_COOKIE, sidebarHidden } from "@/app/_components/sidebar-state";
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

  // read side by side: the sidebar waits on neither alone, and each fails soft to its own row
  const [credits, recent, jar] = await Promise.all([
    consoleFetch<CreditBalance>("credits", ownerId),
    consoleFetch<{ items: RecentRun[] }>(`simulation?limit=${RECENT_LIMIT}`, ownerId),
    cookies(),
  ]);

  return (
    <>
      <DashboardStyles />
      <Shell initialHidden={sidebarHidden(jar.get(SIDEBAR_COOKIE)?.value)}>
        <Sidebar
          credits={credits && typeof credits.balance === "number" ? credits.balance : null}
          email={ownerId}
          recent={recent && Array.isArray(recent.items) ? recent.items : null}
          signOut={<SignOutButton compact />}
        />
        <main className="main">{children}</main>
      </Shell>
    </>
  );
}
