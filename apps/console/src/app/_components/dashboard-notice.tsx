import Link from "next/link";
import React from "react";

import { DashboardRetryButton } from "@/app/_components/dashboard-retry-button";

type DashboardNoticeProps = {
  action?: { href: string; label: string };
  body: string;
  retryLabel?: string;
  title: string;
  tone?: "empty" | "error" | "loading";
};

export function DashboardNotice({
  action,
  body,
  retryLabel,
  title,
  tone = "empty",
}: DashboardNoticeProps) {
  const liveProps =
    tone === "error"
      ? { "aria-live": "assertive" as const, role: "alert" as const }
      : tone === "loading"
        ? { "aria-live": "polite" as const, role: "status" as const }
        : {};

  return (
    <section className={`dashboard-notice dashboard-notice--${tone}`} {...liveProps}>
      <div>
        <h2>{title}</h2>
        <p>{body}</p>
      </div>
      {action || retryLabel ? (
        <div className="dashboard-notice__actions">
          {retryLabel ? <DashboardRetryButton label={retryLabel} /> : null}
          {action ? (
            <Link className="btn" href={action.href}>
              {action.label}
            </Link>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
