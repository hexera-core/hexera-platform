import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { Workbench } from "@/app/_components/workbench";
import { ownerIdFromSession } from "@/lib/auth/session";

export default async function NewRunPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  return <Workbench bootJobId={null} ownerId={ownerId} />;
}
