// Reads the product API's cross-tenant launch-operator surface on the admin console's behalf.

import { hexeraApiRoutes, HexeraApiError, type HexeraApiPath } from "@hexera/api-client";

import { getHexeraApiClient } from "@/lib/hexera-api/server";

export type Read<T> = { data: T; error: null } | { data: null; error: string };

export type AdminRun = {
  attempts: number;
  created_at: string | null;
  ended_at: string | null;
  failed_reason: string | null;
  id: string;
  lease_heartbeat_at: string | null;
  organization_id: string;
  organization_name: string;
  owner_id: string;
  pipeline_backend: string;
  pipeline_dispatch_state: string;
  pipeline_execution_id: string;
  pipeline_launch_error: string;
  pipeline_submitted_at: string | null;
  started_at: string | null;
  status: string;
  task_label: string;
  updated_at: string | null;
};

export type AdminOrganization = {
  active_runs: number;
  balance: number;
  created_at: string | null;
  current_period_end: string | null;
  has_billing_account: boolean;
  id: string;
  included_credits: number;
  last_run_at: string | null;
  name: string;
  plan: string;
  run_count: number;
  slug: string;
  status: string;
  stripe_customer_id: string;
};

export type AdminMember = {
  email: string;
  name: string;
  role: string;
};

export type AdminLedgerEntry = {
  amount: number;
  created_at: string | null;
  entry_type: string;
  id: string;
  metered_at: string | null;
  overage: number;
  reason: string;
};

function ok<T>(data: T): Read<T> {
  return { data, error: null };
}

function failed<T>(error: unknown): Read<T> {
  if (error instanceof HexeraApiError) {
    if (error.status === 404) {
      if (apiDetail(error.body).toLowerCase().includes("organisation not found")) {
        return { data: null, error: "Organization not found." };
      }
      return { data: null, error: "ADMIN_API_KEY is not set on the product API." };
    }
    if (error.status === 403) return { data: null, error: "The admin credential was refused." };
    return { data: null, error: `Product API returned ${error.status}.` };
  }
  return { data: null, error: error instanceof Error ? error.message : String(error) };
}

function apiDetail(body: string): string {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    return typeof parsed.detail === "string" ? parsed.detail : "";
  } catch {
    return body;
  }
}

function adminHeaders(env: NodeJS.ProcessEnv = process.env): HeadersInit {
  return { "X-Admin-Key": env.ADMIN_API_KEY ?? "" };
}

async function request<T>(path: HexeraApiPath, init: RequestInit = {}): Promise<Read<T>> {
  try {
    return ok(
      await getHexeraApiClient().request<T>(path, {
        cache: "no-store",
        ...init,
        headers: { ...adminHeaders(), ...(init.headers ?? {}) },
      }),
    );
  } catch (error) {
    return failed<T>(error);
  }
}

export async function readActivity(): Promise<Read<{ runs: AdminRun[] }>> {
  return request(hexeraApiRoutes.adminOpsActivity);
}

export async function readCustomers(): Promise<
  Read<{ billing_enabled: boolean; organizations: AdminOrganization[] }>
> {
  return request(hexeraApiRoutes.adminOpsOrganizations(0));
}

export async function readCustomer(
  organizationId: string,
): Promise<
  Read<{
    billing_enabled: boolean;
    ledger: AdminLedgerEntry[];
    members: AdminMember[];
    organization: AdminOrganization;
    recent_runs: AdminRun[];
  }>
> {
  return request(hexeraApiRoutes.adminOpsOrganization(organizationId));
}

export async function grantCredits(input: {
  amount: number;
  operationId: string;
  organizationId: string;
  reason: string;
}): Promise<Read<{ balance: number }>> {
  return request(hexeraApiRoutes.adminOpsOrganizationCreditGrants(input.organizationId), {
    body: JSON.stringify({
      amount: input.amount,
      operation_id: input.operationId,
      reason: input.reason,
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export function sortCustomers(
  rows: readonly AdminOrganization[],
  sort: string,
  direction: string,
): AdminOrganization[] {
  const dir = direction === "asc" ? 1 : -1;
  const value = (row: AdminOrganization): string | number => {
    switch (sort) {
      case "active":
        return row.active_runs;
      case "balance":
        return row.balance;
      case "created":
        return row.created_at ?? "";
      case "last-run":
        return row.last_run_at ?? "";
      case "plan":
        return row.plan;
      case "runs":
        return row.run_count;
      case "status":
        return row.status;
      default:
        return row.name.toLowerCase();
    }
  };
  return [...rows].sort((a, b) => {
    const left = value(a);
    const right = value(b);
    if (typeof left === "number" && typeof right === "number") return (left - right) * dir;
    return String(left).localeCompare(String(right)) * dir;
  });
}

export function compactDate(iso: string | null): string {
  if (!iso) return "-";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "-";
  return parsed.toISOString().slice(0, 10);
}

export function signedNumber(amount: number): string {
  const formatted = new Intl.NumberFormat("en-US").format(Math.abs(amount));
  if (amount === 0) return "0";
  return amount > 0 ? `+${formatted}` : `-${formatted}`;
}
