# Responsibility: Prove from inside a container that this deployment can dispatch a mesh, before the stack starts.
# Boundaries: read-only; it describes what exists and never mutates a cloud resource.
from __future__ import annotations

import json
import os
import sys

FAILURES: list[str] = []


def _fail(what: str, fix: str) -> None:
    FAILURES.append(f"{what}\n         fix: {fix}")


def main() -> int:
    import meshpipeline.settings.providers as p

    missing = [n for n, v in (("GCP_PROJECT_ID", p.GCP_PROJECT_ID),
                              ("GCP_REGION", p.GCP_REGION),
                              ("CLOUDRUN_JOB", p.CLOUDRUN_JOB),
                              ("GCP_MESH_BUCKET", p.GCP_MESH_BUCKET)) if not v]
    if missing:
        _fail(f"the mesh job is not configured: {', '.join(missing)} unset",
              "set them in .env (make mesh-setup provisions them for a blank project)")
        return _report()

    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not os.path.exists(cred_path):
        _fail(f"no credential is mounted at {cred_path} in the container",
              "put your credential at secrets/gcp/application_default_credentials.json")
        return _report()

    try:
        import google.auth
        from google.auth.transport.requests import Request
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
    except Exception as exc:
        _fail(f"the mounted credential is not usable: {type(exc).__name__}: {exc}",
              "replace secrets/gcp/application_default_credentials.json, then: make dev-up")
        return _report()

    import urllib.error
    import urllib.request

    def _get(url: str):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {creds.token}"})
        return urllib.request.urlopen(req, timeout=30)

    job = (f"https://run.googleapis.com/v2/projects/{p.GCP_PROJECT_ID}"
           f"/locations/{p.GCP_REGION}/jobs/{p.CLOUDRUN_JOB}")
    try:
        _get(job)
    except urllib.error.HTTPError as exc:
        _fail(f"cannot describe Cloud Run job '{p.CLOUDRUN_JOB}' "
              f"in {p.GCP_PROJECT_ID}/{p.GCP_REGION} (HTTP {exc.code})",
              "check CLOUDRUN_JOB/GCP_PROJECT_ID/GCP_REGION in .env, and that this identity "
              "holds run.jobs.get - 'make mesh-setup' provisions and grants it")
    except Exception as exc:
        _fail(f"cannot reach the Cloud Run API: {type(exc).__name__}: {exc}",
              "check network access to run.googleapis.com")

    want = ["storage.objects.create", "storage.objects.get", "storage.objects.delete"]
    perms = ("https://storage.googleapis.com/storage/v1/b/"
             f"{p.GCP_MESH_BUCKET}/iam/testPermissions?"
             + "&".join(f"permissions={x}" for x in want))
    try:
        granted = set(json.load(_get(perms)).get("permissions", []))
        if missing_perms := [x for x in want if x not in granted]:
            _fail(f"this identity lacks {', '.join(missing_perms)} on "
                  f"gs://{p.GCP_MESH_BUCKET}",
                  "grant roles/storage.objectUser on the exchange bucket "
                  "(make mesh-setup applies it)")
    except urllib.error.HTTPError as exc:
        _fail(f"cannot check access to gs://{p.GCP_MESH_BUCKET} (HTTP {exc.code})",
              "check GCP_MESH_BUCKET in .env and that the bucket exists")
    except Exception as exc:
        _fail(f"cannot reach the exchange bucket: {type(exc).__name__}: {exc}",
              "check network access to storage.googleapis.com")

    # The composed port is the submission authority WRAPPING the Cloud Run executor - nothing may
    # reach the provider except through it, so both halves are checked.
    from meshpipeline.runtime.composition import build_mesh_executor
    composed = build_mesh_executor()
    inner = getattr(composed, "_inner", None)
    if type(composed).__name__ != "ClaimingMeshExecutor" \
            or type(inner).__name__ != "CloudRunMeshExecutor":
        _fail("the container did not compose the remote mesh executor",
              "this is a build defect - report it; do not work around it")

    return _report()


def _report() -> int:
    if not FAILURES:
        return 0
    sys.stderr.write("\n  The stack was not started - meshing would not work:\n\n")
    for f in FAILURES:
        sys.stderr.write(f"    - {f}\n")
    sys.stderr.write("\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
