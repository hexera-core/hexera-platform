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

// THE OPERATOR REPAIR QUEUE
//
// One list over every customer's repair jobs, for this service's own staff. The API answers
// `awaiting_human` / `blocked` / `settled` per row rather than leaving each screen to decide what
// a status means, so the presentation below groups by what it was TOLD, never by re-deriving it.

export type RepairJobRow = {
  assigned_at: string | null;
  assigned_operator: string | null;
  awaiting_human: boolean;
  blocked: boolean;
  blocked_reason: string | null;
  created_at: string | null;
  current_strategy: string;
  id: string;
  organization_id: string | null;
  owner_id: string;
  repair_status: string;
  service_priority: number;
  settled: boolean;
  status: string;
  target_engine: string;
  updated_at: string | null;
};

export type RepairAttemptRow = {
  attempt_no: number;
  caps: Record<string, unknown> | null;
  created_at: string | null;
  input_sha256: string;
  measurements: Record<string, unknown> | null;
  mode: string;
  output_sha256: string | null;
  profile: string;
  report: Record<string, unknown> | null;
  status: string;
  tool_version: string;
};

export type RepairDecisionRow = {
  actor: string;
  created_at: string | null;
  decision: string;
  from_status: string;
  notes: string | null;
  reason: string | null;
};

export type RepairJobDetail = {
  attempts: RepairAttemptRow[];
  available_decisions: string[];
  decisions: RepairDecisionRow[];
  job: RepairJobRow;
};

export async function readRepairQueue(query: {
  limit?: number;
  operator?: string;
  status?: string;
  unassigned?: boolean;
} = {}): Promise<Read<{ filter: Record<string, unknown>; jobs: RepairJobRow[] }>> {
  return request(hexeraApiRoutes.adminRepairQueue(query));
}

export async function readRepairJob(jobId: string): Promise<Read<RepairJobDetail>> {
  return request(hexeraApiRoutes.adminRepairJob(jobId));
}

export async function assignRepairJob(input: {
  claim?: boolean;
  jobId: string;
  operator: string;
}): Promise<Read<{ job: RepairJobRow }>> {
  return request(hexeraApiRoutes.adminRepairJobAssign(input.jobId), {
    body: JSON.stringify({ claim: input.claim ?? true, operator: input.operator }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

export async function decideRepairJob(input: {
  actor: string;
  decision: string;
  jobId: string;
  notes?: string;
  reason?: string;
  strategy?: string;
}): Promise<Read<{ applied: string; decisions: RepairDecisionRow[]; job: RepairJobRow }>> {
  return request(hexeraApiRoutes.adminRepairJobDecide(input.jobId), {
    body: JSON.stringify({
      actor: input.actor,
      decision: input.decision,
      notes: input.notes ?? "",
      reason: input.reason ?? "",
      strategy: input.strategy ?? "",
    }),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

// WHAT THE OPERATOR IS ASKED TO DO, in the words of the decision rather than the state. A button
// reading "awaiting_strategy" tells nobody what clicking it means.
export const REPAIR_DECISION_LABELS: Readonly<Record<string, string>> = {
  ask_customer: "Ask the customer",
  block: "Stop — cannot proceed",
  choose_strategy: "Choose a strategy",
  deliver: "Deliver mesh",
  deliver_repair: "Deliver repaired CAD only",
  escalate: "Escalate",
  inspect: "Inspect",
  manual_cleanup: "Send to manual cleanup",
  mesh: "Mesh it",
  repair: "Run repair",
  retry: "Try again",
  review_mesh: "Review the mesh",
  review_repair: "Review the repair",
};

// The decisions that stop or hold a customer's job, which the API refuses without a reason. The
// form uses this to REQUIRE the field rather than letting the operator discover it as a 422.
export const REPAIR_DECISIONS_NEEDING_REASON: readonly string[] = [
  "ask_customer",
  "block",
  "escalate",
  "manual_cleanup",
];

export function repairDecisionLabel(decision: string): string {
  return REPAIR_DECISION_LABELS[decision] ?? decision.replace(/_/g, " ");
}

export type RepairQueueGroup = {
  jobs: RepairJobRow[];
  key: "blocked" | "ours" | "settled" | "waiting";
  title: string;
};

export function groupRepairQueue(rows: readonly RepairJobRow[]): RepairQueueGroup[] {
  // GROUPED BY WHO IS HOLDING IT UP, because that is what an operator opening this page needs to
  // know first - not alphabetical order and not raw status. Classification comes from the API's
  // own flags so this page and the service metrics cannot disagree about what a state means.
  const groups: RepairQueueGroup[] = [
    { jobs: [], key: "ours", title: "Ours to move" },
    { jobs: [], key: "waiting", title: "Waiting on a person" },
    { jobs: [], key: "blocked", title: "Blocked" },
    { jobs: [], key: "settled", title: "Settled" },
  ];
  const bucket = (row: RepairJobRow): RepairQueueGroup["key"] => {
    if (row.settled) return "settled";
    if (row.blocked) return "blocked";
    if (row.awaiting_human) return "waiting";
    return "ours";
  };
  for (const row of rows) {
    const key = bucket(row);
    groups.find((g) => g.key === key)?.jobs.push(row);
  }
  return groups.filter((g) => g.jobs.length > 0);
}

export function repairStatusLabel(status: string): string {
  return status.replace(/_/g, " ");
}
