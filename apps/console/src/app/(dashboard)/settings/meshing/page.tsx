import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { DashboardNotice } from "@/app/_components/dashboard-notice";
import { type MeshingStandard, SCOPE } from "@/app/_components/meshing-standard";
import { MeshingStandardPanel } from "@/app/_components/meshing-standard-panel";
import { REPAIR_TITLE, type RepairSettings } from "@/app/_components/repair-settings";
import { RepairSettingsPanel } from "@/app/_components/repair-settings-panel";
import { ownerIdFromSession } from "@/lib/auth/session";
import { consoleFetch } from "@/lib/hexera-api/console-fetch";

export default async function MeshingPage() {
  const ownerId = ownerIdFromSession(await auth());
  if (!ownerId) {
    redirect("/sign-in");
  }
  const [standard, repair] = await Promise.all([
    consoleFetch<MeshingStandard>("organization/meshing-standard", ownerId),
    consoleFetch<RepairSettings>("organization/repair-settings", ownerId),
  ]);

  return (
    <div className="page">
      <div className="page__head">
        <div>
          <p className="page__eyebrow">Workspace</p>
          <h1 className="page__title">Meshing</h1>
          <p className="page__intro">{SCOPE}</p>
        </div>
      </div>

      {standard === null ? (
        <DashboardNotice
          body="Refresh the page to try again. Runs keep using the standard already saved."
          retryLabel="Try again"
          title="We couldn't load your meshing standard"
          tone="error"
        />
      ) : (
        <section className="panel">
          <div className="panel__body">
            <MeshingStandardPanel standard={standard} />
          </div>
        </section>
      )}

      {/* THE CAD REPAIR SWITCH: shown when the API serves it; a deployment without it shows nothing rather than an
          error, since runs keep their default (review larger repairs) either way */}
      {repair === null ? null : (
        <section aria-labelledby="repair-settings-title" className="panel">
          <div className="panel__head">
            <div>
              <p className="label" id="repair-settings-title">
                {REPAIR_TITLE}
              </p>
            </div>
          </div>
          <div className="panel__body">
            <RepairSettingsPanel settings={repair} />
          </div>
        </section>
      )}
    </div>
  );
}
