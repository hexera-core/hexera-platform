"use server";

import { revalidatePath } from "next/cache";
import { headers } from "next/headers";

import { getIapVerifier, iapAudience, verifyIapActor, type IapActor } from "@/lib/auth/iap";
import { readAdminTargets } from "@/lib/gcp/config";

import { applyRepairAssignment, applyRepairDecision } from "./operations";

/** What the form gets back. A refusal is RETURNED, never thrown.
 *
 * A lost claim and a status that moved under the operator are ordinary answers on a queue two
 * people work at once - a 409, not a fault. Throwing turned those into the error page and took
 * the typed reason and notes with it, so the operator retyped a paragraph to find out whether
 * anything had changed. The refusal comes back beside what they wrote instead.
 */
export type RepairFormState = {
  error?: string;
  notes?: string;
  ok?: boolean;
  reason?: string;
  strategy?: string;
};

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

function submitted(formData: FormData): Omit<RepairFormState, "error" | "ok"> {
  return {
    notes: String(formData.get("notes") ?? ""),
    reason: String(formData.get("reason") ?? ""),
    strategy: String(formData.get("strategy") ?? ""),
  };
}

export async function decideRepair(
  _previous: RepairFormState,
  formData: FormData,
): Promise<RepairFormState> {
  const jobId = String(formData.get("job_id") ?? "");
  const actor = await verifiedActor();
  const result = await applyRepairDecision(formData, { actor });
  if (result.error) {
    // NOTHING HAPPENED, so nothing is revalidated: re-reading the job here would replace the
    // page the operator is looking at while telling them their decision was refused.
    return { error: result.error, ...submitted(formData) };
  }
  // BOTH PAGES. The queue's grouping is derived from the status this decision just changed, so
  // leaving it cached would show the operator their job in the column it was in a moment ago.
  revalidatePath("/repair");
  revalidatePath(`/repair/${jobId}`);
  return { ok: true };
}

export async function claimRepair(
  _previous: RepairFormState,
  formData: FormData,
): Promise<RepairFormState> {
  const jobId = String(formData.get("job_id") ?? "");
  const actor = await verifiedActor();
  const result = await applyRepairAssignment(formData, { actor });
  if (result.error) {
    return { error: result.error };
  }
  revalidatePath("/repair");
  revalidatePath(`/repair/${jobId}`);
  return { ok: true };
}
