// THE AUDIT TRAIL. One structured line per infrastructure change, written to stdout.
//
// WHY STDOUT AND NOT A TABLE. The admin console holds no database connection - the fleet pages
// read GCP's APIs and nothing else, which is what let them ship without a VPC connector and a
// Postgres role. Cloud Run ingests a JSON object on stdout into Cloud Logging with its severity
// and fields intact, so this is a durable, queryable, retained record that costs no new
// infrastructure. It is also outside the console's own reach, which is a property a table in a
// database the console can write would not have.

export type AdminAuditEntry = {
  action: string;
  actor: string;
  after?: unknown;
  before?: unknown;
  operationId?: string | null;
  resource: string;
};

export function recordAdminAction(
  entry: AdminAuditEntry,
  write: (line: string) => void = console.log,
  onWriteFailure: (error: unknown) => void = (error) =>
    console.error("admin audit line could not be written", error),
): void {
  const payload = {
    action: entry.action,
    actor: entry.actor,
    after: entry.after ?? null,
    before: entry.before ?? null,
    "logging.googleapis.com/labels": { component: "admin-console", kind: "admin-action" },
    message: `${entry.actor} performed ${entry.action} on ${entry.resource}`,
    operationId: entry.operationId ?? null,
    resource: entry.resource,
    severity: "NOTICE",
    timestamp: new Date().toISOString(),
  };

  try {
    write(JSON.stringify(payload));
  } catch (error) {
    // A mutation whose audit entry cannot be written still proceeds. Refusing an infrastructure
    // change because logging is degraded is the wrong failure - but the failure is itself
    // reported, so a silently unlogged change is not a state this can reach.
    onWriteFailure(error);
  }
}
