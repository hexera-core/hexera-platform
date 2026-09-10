"use client";

// WHAT A FAILED READ LOOKS LIKE. Every page in this console reads Google's APIs, so the errors an
// operator will actually meet are permission and configuration failures - not bugs. The default
// error page says "something went wrong", which sends someone to the logs for an answer the error
// itself already contains.
export default function AdminError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <>
      <h1>This page could not be read</h1>
      <p className="admin-alert">{error.message}</p>
      <p className="admin-empty">
        These pages read the Compute, Monitoring, Cloud Run and Billing APIs as the admin service
        account. A <code>PERMISSION_DENIED</code> here means that account is missing one of the
        viewer roles <code>create-admin-service.sh</code> grants it. A <code>NOT_FOUND</code> means
        the resource this deployment names does not exist in this project — check{" "}
        <code>WORKER_MIG</code>, <code>WORKER_MIG_ZONE</code> and{" "}
        <code>CLOUDRUN_API_SERVICE</code> against what is actually deployed.
      </p>
      {error.digest ? <p className="admin-empty">Log correlation id: {error.digest}</p> : null}
      <p style={{ marginTop: "1rem" }}>
        <button className="admin-button" onClick={reset} type="button">
          Try again
        </button>
      </p>
    </>
  );
}
