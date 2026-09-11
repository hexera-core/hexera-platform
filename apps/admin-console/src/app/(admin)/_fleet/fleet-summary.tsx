import { Alert, Panel, StatRow } from "@/app/_components/panel";
import type { FleetSummary } from "@/lib/gcp/instances";

// What the instance table adds up to, read before the table itself. An operator arriving at this
// page wants the shape of the fleet before its rows.
export function FleetSummaryPanel({
  summary,
  targetSize,
}: {
  summary: FleetSummary;
  targetSize: number;
}) {
  return (
    <Panel title="Fleet right now">
      {summary.rotating ? (
        <Alert tone="info">
          Rotation in flight — {summary.templateVersions.length} template versions running:{" "}
          {summary.templateVersions.join(", ")}.
        </Alert>
      ) : null}
      {summary.unhealthy > 0 ? (
        <Alert>
          {summary.unhealthy} instance{summary.unhealthy === 1 ? "" : "s"} failing a health check.
        </Alert>
      ) : null}
      <StatRow
        stats={[
          { label: "Instances", value: String(summary.total) },
          { label: "Target", value: String(targetSize) },
          ...summary.byStatus.map((entry) => ({
            label: entry.label.toLowerCase(),
            value: String(entry.count),
          })),
          ...summary.byAction.map((entry) => ({
            label: `${entry.label.toLowerCase()} now`,
            value: String(entry.count),
          })),
        ]}
      />
    </Panel>
  );
}
