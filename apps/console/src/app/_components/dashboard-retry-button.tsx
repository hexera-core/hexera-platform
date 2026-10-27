"use client";

import { useRouter } from "next/navigation";
import React from "react";

type RetryButtonProps = {
  label: string;
  onRetry: () => void;
};

export function RetryButton({ label, onRetry }: RetryButtonProps) {
  return (
    <button className="btn" onClick={onRetry} type="button">
      {label}
    </button>
  );
}

export function DashboardRetryButton({ label }: { label: string }) {
  const router = useRouter();
  return <RetryButton label={label} onRetry={() => router.refresh()} />;
}
