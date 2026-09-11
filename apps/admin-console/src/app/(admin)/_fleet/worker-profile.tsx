import { FieldList, Panel, StatRow } from "@/app/_components/panel";
import type { WorkerProfile } from "@/lib/gcp/worker-profile";

// What a worker is. Metadata KEYS are listed and their values are not - see worker-profile.ts for
// why, and for the single allowlisted exception this panel does render.
export function WorkerProfilePanel({ profile }: { profile: WorkerProfile }) {
  return (
    <Panel heading={profile.templateName} title="Worker profile">
      <StatRow
        stats={[
          { label: "Machine", value: profile.machineType ?? "—" },
          {
            label: "Boot disk",
            value: profile.bootDiskGb
              ? `${profile.bootDiskGb} GB ${profile.bootDiskType ?? ""}`.trim()
              : "—",
          },
          { label: "OS image", value: profile.osImage ?? "—" },
          {
            label: "Network",
            value: `${profile.network ?? "—"} / ${profile.subnetwork ?? "—"}`,
          },
        ]}
      />
      <FieldList
        fields={[
          { label: "Application image", value: profile.workerImage ?? "—" },
          { label: "Identity", value: profile.serviceAccount ?? "—" },
          { label: "Scopes", value: profile.scopes.join(", ") || "—" },
          { label: "Metadata keys", value: profile.metadataKeys.join(", ") || "—" },
        ]}
      />
      <p className="admin-empty">
        Metadata values are hidden — instance metadata is world-readable to anyone with
        compute.instances.get. Shape belongs to the deploy, not this console.
      </p>
    </Panel>
  );
}
