// The Files panel's rows, as data. Pure, so what the panel shows - and what it never shows - is
// testable without rendering it.

/** One artifact as the run read serves it (api/schemas/job.ArtifactOut). */
export type RunArtifact = {
  id: string;
  artifact_type: string;
  label?: string;
  size_bytes?: number;
};

/** The run read, as much of it as the panel needs. */
export type RunRead = {
  id: string;
  status: string;
  artifacts?: RunArtifact[];
};

/** How often the panel reads the run again while it is still working. */
export const FILES_POLL_MS = 10_000;

/** The states still moving: the panel keeps reading while the run is in one. */
const LIVE = new Set(["running", "pending", "queued", "pending_review"]);

/** WHAT THE USER TAKES AWAY. Everything else a run stores (the viewer's data, a video) is ours, not
 *  theirs, and never shows. A type this list does not know stays out too: a new internal artifact
 *  must not appear in a customer's panel by default. */
const USER_FACING: Record<string, { order: number; fallback: string }> = {
  mesh_bundle: { order: 0, fallback: "Everything" },
  mesh: { order: 1, fallback: "Mesh" },
  repaired_cad: { order: 2, fallback: "Repaired CAD" },
  repair_report: { order: 3, fallback: "CAD inspection report" },
};

export function isLive(status: string | undefined): boolean {
  return LIVE.has(String(status || ""));
}

/** "212 MB", "35.1 KB", "900 B"; empty when the size is unknown. */
export function sizeWords(bytes: number | undefined): string {
  const b = Number(bytes) || 0;
  if (b <= 0) {
    return "";
  }
  if (b < 1024) {
    return `${b} B`;
  }
  if (b < 1024 * 1024) {
    return `${(b / 1024).toFixed(1)} KB`;
  }
  if (b < 1024 * 1024 * 1024) {
    return `${(b / (1024 * 1024)).toFixed(b < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  }
  return `${(b / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export type FileRow = { id: string; label: string; size: string; main: boolean };

/** The rows the panel shows: user-facing files only, the whole bundle first. */
export function fileRows(artifacts: RunArtifact[] | undefined): FileRow[] {
  return (artifacts || [])
    .filter((a) => a && a.id && a.artifact_type in USER_FACING)
    .sort((a, b) => USER_FACING[a.artifact_type].order - USER_FACING[b.artifact_type].order)
    .map((a) => ({
      id: a.id,
      label: (a.label || "").trim() || USER_FACING[a.artifact_type].fallback,
      size: sizeWords(a.size_bytes),
      main: a.artifact_type === "mesh_bundle",
    }));
}

/** The run a path belongs to: "/runs/<id>" gives the id; "/runs/new" and anything else, none. */
export function runIdFrom(pathname: string): string | null {
  const m = /^\/runs\/([^/?#]+)/.exec(pathname || "");
  if (!m || m[1] === "new") {
    return null;
  }
  return decodeURIComponent(m[1]);
}

/** What the panel says when there is nothing to list. */
export function emptyWords(runId: string | null, status: string | undefined): string {
  if (!runId) {
    return "Files show up here once a run starts.";
  }
  if (isLive(status)) {
    return "Files show up here as Hexera writes them.";
  }
  return status === "failed" || status === "cancelled"
    ? "This run did not deliver any files."
    : "No files for this run yet.";
}
