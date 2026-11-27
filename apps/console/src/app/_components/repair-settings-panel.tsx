"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { type RepairSettings, repairSaveFailure } from "@/app/_components/repair-settings";
import { RepairSettingsView } from "@/app/_components/repair-settings-view";

/** The switch saves as it is flipped: one choice, nothing to type, so there is no Save button to forget. */
export function RepairSettingsPanel({ settings }: { settings: RepairSettings }) {
  const router = useRouter();
  const [saved, setSaved] = useState<RepairSettings>(settings);
  const [on, setOn] = useState(settings.auto_accept_repairs);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle(next: boolean) {
    setOn(next);
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/organization/repair-settings", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ auto_accept_repairs: next }),
      });
      if (!response.ok) {
        setOn(saved.auto_accept_repairs);
        setError(repairSaveFailure(response.status));
        return;
      }
      const body = (await response.json()) as RepairSettings;
      setSaved(body);
      setOn(body.auto_accept_repairs);
      router.refresh();
    } catch {
      setOn(saved.auto_accept_repairs);
      setError(repairSaveFailure(0));
    } finally {
      setBusy(false);
    }
  }

  return <RepairSettingsView busy={busy} error={error} on={on} onToggle={toggle} saved={saved} />;
}
