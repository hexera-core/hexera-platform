"use server";

import { revalidatePath } from "next/cache";
import { headers } from "next/headers";

import { getIapVerifier, iapAudience, verifyIapActor } from "@/lib/auth/iap";
import { getFleetWriteClients, getRunWriter } from "@/lib/gcp/clients";
import { readAdminTargets } from "@/lib/gcp/config";

import { applyFleetOperation, type ActionResult } from "./operations";

// THE ONE ENTRY POINT for every control on the Fleet page.
//
// A Server Function is reachable by a direct POST, not only through the form that renders it, so
// the authorisation here is not a convenience for the UI - it is the only thing between an
// unauthenticated request and a VM deletion. It runs before the operation is even read.
//
// Everything that decides WHAT happens is in operations.ts, which takes its clients as parameters
// and is tested against fakes. This file holds only what needs a live request: the headers, the
// IAP verification and the revalidation.

export async function fleetControlAction(
  _previous: ActionResult,
  form: FormData,
): Promise<ActionResult> {
  const targets = readAdminTargets(process.env);

  let actor;
  try {
    const headerList = await headers();
    actor = await verifyIapActor({
      assertion: headerList.get("x-goog-iap-jwt-assertion"),
      audience: iapAudience({
        projectNumber: targets.projectNumber,
        region: targets.region,
        service: targets.adminService,
      }),
      verifier: getIapVerifier(),
    });
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), ok: false };
  }

  const result = await applyFleetOperation({
    actor,
    clients: getFleetWriteClients(),
    form,
    runClient: getRunWriter(),
    targets,
  });

  // Compute mutations are long-running operations that this deliberately does not await.
  // Revalidating re-reads the group, which is where the change becomes visible - as a currentActions
  // counter first, and as a new size once the operation completes.
  if (result.ok) revalidatePath("/");

  return result;
}
