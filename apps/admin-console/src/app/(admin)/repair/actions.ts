"use server";

import { revalidatePath } from "next/cache";
import { headers } from "next/headers";

import { getIapVerifier, iapAudience, verifyIapActor, type IapActor } from "@/lib/auth/iap";
import { readAdminTargets } from "@/lib/gcp/config";

import {
  applyRepairAssignment,
  applyRepairDecision,
  type RepairActionResult,
} from "./operations";

async function verifiedActor(): Promise<IapActor> {
  const targets = readAdminTargets(process.env);
  const headerList = await headers();
  return verifyIapActor({
    assertion: headerList.get("x-goog-iap-jwt-assertion"),
    audience: iapAudience({
      projectNumber: targets.projectNumber,
      region: targets.region,
      service: targets.adminService,
    }),
    verifier: getIapVerifier(),
  });
}

function throwIfFailed(result: RepairActionResult): void {
  if (result.error) {
    throw new Error(result.error);
  }
}

export async function decideRepair(formData: FormData) {
  const jobId = String(formData.get("job_id") ?? "");
  const actor = await verifiedActor();
  throwIfFailed(await applyRepairDecision(formData, { actor }));
  // BOTH PAGES. The queue's grouping is derived from the status this decision just changed, so
  // leaving it cached would show the operator their job in the column it was in a moment ago.
  revalidatePath("/repair");
  revalidatePath(`/repair/${jobId}`);
}

export async function claimRepair(formData: FormData) {
  const jobId = String(formData.get("job_id") ?? "");
  const actor = await verifiedActor();
  throwIfFailed(await applyRepairAssignment(formData, { actor }));
  revalidatePath("/repair");
  revalidatePath(`/repair/${jobId}`);
}
