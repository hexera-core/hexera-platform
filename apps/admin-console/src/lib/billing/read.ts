// Reads the product API's cross-tenant billing surface on the admin console's behalf.
//
// THE CREDENTIAL IS NOT MESH_API_KEY. These routes read every tenant's ledger at once, so they sit
// behind their own secret (ADMIN_API_KEY) that the console, the worker and the mesh executor do not
// hold. Sending the wrong one gets a 403, not a partial answer.
//
// EVERY READ RETURNS ITS OWN ERROR rather than throwing, following the Costs page: these sit behind
// different states - an unconfigured provider, a product API that is down, a deployment with no
// admin key - and a Billing page that renders one failed panel is strictly more useful than one
// that renders a stack trace instead of the three panels that did work.

import { hexeraApiRoutes, HexeraApiError } from "@hexera/api-client";

import { getHexeraApiClient } from "@/lib/hexera-api/server";

export type BillingOrganization = {
  balance: number;
  created_at: string | null;
  current_period_end: string | null;
  has_billing_account: boolean;
  id: string;
  included_credits: number;
  name: string;
  plan: string;
  slug: string;
  status: string;
  stripe_customer_id: string;
};

export type BillingPlan = {
  included_credits: number;
  max_concurrent_jobs: number;
  max_jobs_per_owner: number;
  name: string;
  purchasable: boolean;
  rate_limit_per_minute: number;
};

export type UsagePeriod = {
  entries: number;
  granted: number;
  period: string | null;
  spent: number;
};

export type LedgerEntry = {
  amount: number;
  created_at: string | null;
  entry_type: string;
  id: string;
  metered_at: string | null;
  reason: string;
};

export type InvoiceRow = {
  amount_due: number;
  amount_paid: number;
  created_at: string | null;
  currency: string;
  hosted_url: string;
  id: string;
  number: string;
  status: string;
};

export type Read<T> = { data: T; error: null } | { data: null; error: string };

function ok<T>(data: T): Read<T> {
  return { data, error: null };
}

function failed<T>(error: unknown): Read<T> {
  // THE STATUS IS NAMED WHERE WE HAVE ONE, because the three that matter here mean three different
  // fixes: 404 is "this deployment has no admin key set", 403 is "the console's key is wrong", and
  // 503 is "no payment provider is configured". Collapsing them to "request failed" sends an
  // operator to read logs for something the page already knew.
  if (error instanceof HexeraApiError) {
    if (error.status === 404) {
      return { data: null, error: "ADMIN_API_KEY is not set on the product API." };
    }
    if (error.status === 403) {
      return { data: null, error: "The admin credential was refused (ADMIN_API_KEY mismatch)." };
    }
    if (error.status === 503) {
      return { data: null, error: "No payment provider is configured (STRIPE_API_KEY unset)." };
    }
    return { data: null, error: `Product API returned ${error.status}.` };
  }
  return { data: null, error: error instanceof Error ? error.message : String(error) };
}

function adminHeaders(env: NodeJS.ProcessEnv = process.env): HeadersInit {
  return { "X-Admin-Key": env.ADMIN_API_KEY ?? "" };
}

async function get<T>(path: Parameters<ReturnType<typeof getHexeraApiClient>["request"]>[0]): Promise<Read<T>> {
  try {
    return ok(
      await getHexeraApiClient().request<T>(path, {
        // NO CACHING. A balance that is one render stale is a number an operator will act on, and
        // Next will happily serve a cached fetch for a full route segment lifetime otherwise.
        cache: "no-store",
        headers: adminHeaders(),
      }),
    );
  } catch (error) {
    return failed<T>(error);
  }
}

export async function readOrganizations(): Promise<
  Read<{ billing_enabled: boolean; organizations: BillingOrganization[] }>
> {
  return get(hexeraApiRoutes.adminBillingOrganizations);
}

export async function readPlans(): Promise<Read<{ billing_enabled: boolean; plans: BillingPlan[] }>> {
  return get(hexeraApiRoutes.adminBillingPlans);
}

export async function readUsage(): Promise<
  Read<{ pending_meter: { credits: number; entries: number }; periods: UsagePeriod[] }>
> {
  return get(hexeraApiRoutes.adminBillingUsage);
}

export async function readLedger(organizationId: string): Promise<Read<{ entries: LedgerEntry[] }>> {
  return get(hexeraApiRoutes.adminBillingLedger(organizationId));
}

export async function readInvoices(
  organizationId: string,
): Promise<Read<{ invoices: InvoiceRow[]; reason?: string }>> {
  return get(hexeraApiRoutes.adminBillingInvoices(organizationId));
}

// FORMATTING lives here rather than in the page so the same number reads the same way in every
// panel, and so the rules below are testable without rendering React.

export function money(amountMinor: number, currency: string): string {
  // THE PROVIDER SENDS THE SMALLEST UNIT (cents), which is why this divides rather than trusting a
  // decimal. A zero-decimal currency like JPY would be wrong here - it is called out rather than
  // silently mishandled, because the fix is a currency table this deployment does not need yet.
  const value = amountMinor / 100;
  try {
    return new Intl.NumberFormat("en-US", {
      currency: (currency || "usd").toUpperCase(),
      style: "currency",
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency.toUpperCase()}`.trim();
  }
}

export function credits(amount: number): string {
  // SIGNED, always, including the plus. A ledger reader scans a column of numbers; dropping the
  // sign on grants makes a debit and a grant look identical at a glance.
  const formatted = new Intl.NumberFormat("en-US").format(Math.abs(amount));
  if (amount === 0) return "0";
  return amount > 0 ? `+${formatted}` : `-${formatted}`;
}

export function when(iso: string | null): string {
  if (!iso) return "—";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toISOString().slice(0, 10);
}

export function planLabel(plan: string): string {
  // AN EMPTY PLAN IS "free", NAMED. Rendering a blank cell would read as missing data, when it is
  // in fact the most common and entirely correct state: settings/plans.py resolves an empty plan to
  // the deployment's own limits.
  return plan.trim() === "" ? "free" : plan;
}
