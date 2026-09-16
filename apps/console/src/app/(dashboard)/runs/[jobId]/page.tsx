import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { Workbench } from "@/app/_components/workbench";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function RunPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const { jobId } = await params;
  // The id is handed to main.js as a global rather than checked here. bootLive() already resolves
  // it against the API and says so honestly when the job belongs to somebody else -- duplicating
  // that check server-side would be a second source of truth for the same refusal.
  return <Workbench bootJobId={jobId} ownerId={ownerId} />;
}
