"use client";

// THE LAST RESORT, and deliberately quiet about detail it does not have.
//
// Next REDACTS a Server Component's error message before it reaches a client boundary - this
// component receives "Minified React error #441" and a digest, never the text. Printing that
// message would be printing a React internals reference in the place an operator looks for the
// actual failure, which is worse than printing nothing.
//
// The pages that read Google's APIs therefore catch their own failures on the server, where the
// message is still readable, and render it as content. This boundary only catches what those
// catches did not, so what it can usefully offer is the digest and where to take it.
export default function AdminError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <>
      <h1>This page could not be rendered</h1>
      <p className="admin-empty">
        Something failed outside the reads this console handles itself. The detail is not available
        in the browser — Next strips a server error&rsquo;s message before it reaches this page — so
        it is in Cloud Logging for the admin service, correlated by the id below.
      </p>
      {error.digest ? (
        <p className="admin-alert">
          Log correlation id: <code>{error.digest}</code>
        </p>
      ) : null}
      <p className="admin-empty">
        Read it with:{" "}
        <code>
          gcloud logging read &apos;resource.type=cloud_run_revision AND
          labels.&quot;run.googleapis.com/execution_name&quot;:*&apos; --limit 20
        </code>
      </p>
      <p style={{ marginTop: "1rem" }}>
        <button className="admin-button" onClick={reset} type="button">
          Try again
        </button>
      </p>
    </>
  );
}
