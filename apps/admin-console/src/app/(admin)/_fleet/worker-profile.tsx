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
        Metadata values are not shown. Instance metadata is readable by anyone holding
        compute.instances.get, so this console lists what is set, never what it is set to. The
        application image is the one exception, because which build the fleet runs is the question
        this panel exists to answer.
      </p>
      <p className="admin-empty">
        Machine type, disk, image and identity belong to the deploy, not to this console. Changing
        them means a new instance template and a rolling update, which{" "}
        <code>create-worker-fleet.sh</code> owns.
      </p>
    </Panel>
  );
}
