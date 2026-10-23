"use client";

import Link from "next/link";
import React from "react";

import { RetryButton } from "@/app/_components/dashboard-retry-button";

export default function DashboardError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="page">
      <section className="dashboard-notice dashboard-notice--error" role="alert">
        <div>
          <h2>We couldn&apos;t open this page</h2>
          <p>Try again now. If the problem continues, return to the overview and resume there.</p>
        </div>
        <div className="dashboard-notice__actions">
          <RetryButton label="Try again" onRetry={reset} />
          <Link className="btn" href="/">
            Return to overview
          </Link>
        </div>
      </section>
    </div>
  );
}
