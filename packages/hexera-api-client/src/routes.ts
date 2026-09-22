export const HEXERA_API_PREFIX = "/api/v1" as const;

type PathSegment = string | number;

// THE PATH SHAPE, declared here rather than imported from client.ts, which already imports this
// module - the other direction would be a cycle. `client.ts` re-exports it as `HexeraApiPath`.
//
// The BUILDERS below are annotated with it explicitly. Without the annotation TypeScript widens
// their return to plain `string` - `segment()` returns string, so the template literal loses the
// prefix it provably starts with - and `request()` then refuses the very routes this module exists
// to provide. The constant routes infer it correctly on their own; only the functions need saying.
type ApiPath = `${typeof HEXERA_API_PREFIX}${string}`;

function segment(value: PathSegment): string {
  return encodeURIComponent(String(value));
}

export const hexeraApiRoutes = {
  // THE CROSS-TENANT SURFACE. Every route below `admin` reads across all tenants and is gated by
  // its own credential (X-Admin-Key / ADMIN_API_KEY), which is deliberately NOT the MESH_API_KEY
  // the console and worker hold. Anything calling these is an operator tool, never a customer one.
  adminBillingLedger: (organizationId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/admin/billing/organizations/${segment(organizationId)}/ledger`,
  adminBillingInvoices: (organizationId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/admin/billing/organizations/${segment(organizationId)}/invoices`,
  adminBillingOrganizations: `${HEXERA_API_PREFIX}/admin/billing/organizations`,
  adminBillingPlans: `${HEXERA_API_PREFIX}/admin/billing/plans`,
  adminBillingUsage: `${HEXERA_API_PREFIX}/admin/billing/usage`,
  // The customer-facing billing surface, scoped to the caller's own organisation.
  billing: `${HEXERA_API_PREFIX}/billing`,
  billingCheckout: `${HEXERA_API_PREFIX}/billing/checkout`,
  billingPlans: `${HEXERA_API_PREFIX}/billing/plans`,
  billingPortal: `${HEXERA_API_PREFIX}/billing/portal`,
  chatHistory: (sessionId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/chat/history/${segment(sessionId)}`,
  chatMessage: `${HEXERA_API_PREFIX}/chat/message`,
  clientConfig: `${HEXERA_API_PREFIX}/client-config`,
  simulation: (jobId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/simulation/${segment(jobId)}`,
  simulationDispute: (jobId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/simulation/${segment(jobId)}/dispute`,
  simulationSurface: (jobId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/simulation/${segment(jobId)}/surface`,
  uploadStepFile: `${HEXERA_API_PREFIX}/upload/step-file`,
  wsStream: (jobId: PathSegment): ApiPath =>
    `${HEXERA_API_PREFIX}/ws/${segment(jobId)}/stream`,
  wsTicket: `${HEXERA_API_PREFIX}/ws/ticket`,
} as const;

export type HexeraApiRoute =
  | (typeof hexeraApiRoutes)[keyof typeof hexeraApiRoutes]
  | ReturnType<(typeof hexeraApiRoutes)["adminBillingInvoices"]>
  | ReturnType<(typeof hexeraApiRoutes)["adminBillingLedger"]>
  | ReturnType<(typeof hexeraApiRoutes)["chatHistory"]>
  | ReturnType<(typeof hexeraApiRoutes)["simulation"]>
  | ReturnType<(typeof hexeraApiRoutes)["simulationDispute"]>
  | ReturnType<(typeof hexeraApiRoutes)["simulationSurface"]>
  | ReturnType<(typeof hexeraApiRoutes)["wsStream"]>;
