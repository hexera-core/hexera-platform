import React from "react";

import { DashboardNotice } from "@/app/_components/dashboard-notice";

export default function DashboardLoading() {
  return (
    <div className="page">
      <DashboardNotice
        body="Fetching the latest data for this workspace."
        title="Loading workspace"
        tone="loading"
      />
    </div>
  );
}
