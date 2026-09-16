import type { TemplateReader } from "./clients";
import { lastSegment, toNumber } from "./fleet";

// What a worker IS, read from the instance template the group runs.
//
// METADATA VALUES ARE WITHHELD, AND THE ONE EXCEPTION IS NAMED.
// create-worker-fleet.sh spends four paragraphs establishing that instance metadata is a public
// surface: it is readable by anyone holding compute.instances.get, which is why that script writes
// secret NAMES there and never secret values. The names are not themselves sensitive, but a page
// that prints instance metadata wholesale is one template change away from printing a credential,
// and nothing would catch it. So this reader lists KEYS and withholds VALUES.
//
// `worker-image` is the exception, allowlisted rather than special-cased: it is the pinned
// application digest, and "which build is this fleet running" is the question the panel exists to
// answer. Adding a key here is a deliberate act with a test in front of it.
export const RENDERABLE_METADATA_KEYS: readonly string[] = ["worker-image"];

export type WorkerProfile = {
  bootDiskGb: number | null;
  bootDiskType: string | null;
  machineType: string | null;
  metadataKeys: readonly string[];
  network: string | null;
  osImage: string | null;
  scopes: readonly string[];
  serviceAccount: string | null;
  subnetwork: string | null;
  templateName: string;
  workerImage: string | null;
};

export async function readWorkerProfile(
  clients: { instanceTemplates: TemplateReader },
  projectId: string,
  templateName: string,
): Promise<WorkerProfile> {
  // Instance templates are GLOBAL resources - there is no zone on this request, and passing one
  // is an error rather than a no-op.
  const [template] = await clients.instanceTemplates.get({
    instanceTemplate: templateName,
    project: projectId,
  });

  const properties = template.properties ?? {};
  const boot = properties.disks?.find((disk) => disk.boot) ?? null;
  const account = properties.serviceAccounts?.[0] ?? null;
  const nic = properties.networkInterfaces?.[0] ?? null;
  const items = properties.metadata?.items ?? [];

  const allowed = new Map(
    items
      .filter((item) => item.key && RENDERABLE_METADATA_KEYS.includes(item.key))
      .map((item) => [item.key as string, item.value ?? null]),
  );

  return {
    bootDiskGb: toNumber(boot?.initializeParams?.diskSizeGb),
    bootDiskType: lastSegment(boot?.initializeParams?.diskType),
    machineType: lastSegment(properties.machineType),
    metadataKeys: items
      .map((item) => item.key)
      .filter((key): key is string => Boolean(key))
      .sort(),
    network: lastSegment(nic?.network),
    osImage: lastSegment(boot?.initializeParams?.sourceImage),
    scopes: account?.scopes ?? [],
    serviceAccount: account?.email ?? null,
    subnetwork: lastSegment(nic?.subnetwork),
    templateName: template.name ?? templateName,
    workerImage: allowed.get("worker-image") ?? null,
  };
}
