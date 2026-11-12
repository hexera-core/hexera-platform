"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import {
  type MeshingStandard,
  type ShellElements,
  saveFailureMessage,
  shellElementsOf,
} from "@/app/_components/meshing-standard";
import { MeshingStandardView } from "@/app/_components/meshing-standard-view";

export function MeshingStandardPanel({ standard }: { standard: MeshingStandard }) {
  const router = useRouter();
  // THE SAVED STANDARD IS THE PUT's ANSWER, not a re-read: the answer carries what the reader understood of
  // exactly the words that were saved, so the lines shown can never be of an older text.
  const [saved, setSaved] = useState<MeshingStandard>(standard);
  const [draft, setDraft] = useState(standard.text);
  const [kind, setKind] = useState<ShellElements>(shellElementsOf(standard));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/organization/meshing-standard", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: draft, shell_elements: kind }),
      });
      if (!response.ok) {
        setError(saveFailureMessage(response.status));
        return;
      }
      const body = (await response.json()) as MeshingStandard;
      setSaved(body);
      setDraft(body.text);
      setKind(shellElementsOf(body));
      router.refresh();
    } catch {
      setError(saveFailureMessage(0));
    } finally {
      setBusy(false);
    }
  }

  return (
    <MeshingStandardView
      busy={busy}
      draft={draft}
      error={error}
      kind={kind}
      onChange={setDraft}
      onKind={setKind}
      onClear={() => setDraft("")}
      onSave={save}
      saved={saved}
    />
  );
}
