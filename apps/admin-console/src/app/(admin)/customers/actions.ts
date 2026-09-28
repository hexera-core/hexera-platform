"use server";

import { revalidatePath } from "next/cache";
import { headers } from "next/headers";

import { getIapVerifier, iapAudience, verifyIapActor, type IapActor } from "@/lib/auth/iap";
import { readAdminTargets } from "@/lib/gcp/config";

import { applyCreditGrant, applyInvoiceRaise, type CustomerActionResult } from "./operations";

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

function throwIfFailed(result: CustomerActionResult): void {
  if (result.error) {
    throw new Error(result.error);
  }
}

export async function grantCustomerCredits(formData: FormData) {
  const organizationId = String(formData.get("organization_id") ?? "");
  const actor = await verifiedActor();
  throwIfFailed(await applyCreditGrant(formData, { actor }));
  revalidatePath("/customers");
  revalidatePath(`/customers/${organizationId}`);
}

export async function raiseCustomerInvoice(formData: FormData) {
  const organizationId = String(formData.get("organization_id") ?? "");
  const actor = await verifiedActor();
  throwIfFailed(await applyInvoiceRaise(formData, { actor }));
  revalidatePath("/billing");
  revalidatePath(`/customers/${organizationId}`);
}
