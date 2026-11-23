# Responsibility: The ONE definition of where a CRS v4 wave lives in GCS and who it runs as - the
#                 project, bucket, service account, digest grammar, verdict-cache prefix and results
#                 root - so the fleet launcher and the preflight cannot drift apart.
# Owns: PROJECT, PROJECT_NUMBER, BUCKET, SERVICE_ACCOUNT, DIGEST_RE, V4_PREFIX, `verdict_cache_uri`, `shared_cache_problem`,
#       `results_root`.
# Boundaries: pure constants and string functions; no I/O, no gcloud, no imports from the repo.
"""Shared wave constants. Imported by v3_eval/launch_fleet.py (the authority on what a wave writes)
and by v4_eval/preflight.py (which must validate exactly those locations).

Task 8b's launcher defined these first; they live here now so that the launcher and the preflight read
one definition. A drift test (tests/unit/dev/test_v4_preflight.py) pins the preflight's locations to
the launcher's metadata and results root.
"""
from __future__ import annotations

import re

PROJECT = "hexera-dev"
PROJECT_NUMBER = "224734058693"
#: The v3 eval's transfer bucket; the compute service account already has objectAdmin on it.
BUCKET = f"gs://dev-crsv3-transfer-{PROJECT_NUMBER}"
#: The identity the fleet's VMs run as.
SERVICE_ACCOUNT = f"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
#: An Artifact Registry reference pinned by a full sha256 digest; group 1 is `sha256:<64 hex>`.
DIGEST_RE = re.compile(r"^[a-z0-9.-]+-docker\.pkg\.dev/[^@\s]+@(sha256:[0-9a-f]{64})$")
#: Inputs, pool manifest and verdict cache of the v4 re-probe wave live under this prefix.
V4_PREFIX = f"{BUCKET}/eval/v4-reprobe"
#: The arm name results are filed under (launch_fleet V4 profile: arms=("reprobe",), kinds=("pool",)).
V4_ARM = "reprobe"


def verdict_cache_uri(prefix: str) -> str:
    """The `--cache-uri` a v4 shard receives (launch_fleet's `v4-cache-uri` metadata)."""
    return f"{prefix}/verdict-cache"


def shared_cache_problem(uri: str) -> str | None:
    """Why `uri` cannot be a wave's verdict cache, or None. A wave's shards run on separate VMs, so the cache must
    live in object storage (`gs://<bucket>/<prefix>`): blank means every judge falls back to a per-process
    in-memory cache, and a directory is per-VM. Either looks exactly like a working cache and is a cost control
    silently off. The library keeps its in-memory fallback; only the wave boundary refuses."""
    uri = (uri or "").strip()
    if not uri:
        return "the verdict cache URI is blank: judges would fall back to a per-process in-memory cache"
    rest = uri[len("gs://"):] if uri.startswith("gs://") else ""
    bucket, _, path = rest.partition("/")
    if not bucket or not path.strip("/"):
        return (f"the verdict cache URI {uri!r} is not a shared gs://<bucket>/<prefix> location: a directory or "
                "memory cache is per-VM, so verdicts would not be shared across the wave")
    return None


def results_root(arm: str, commit: str, bucket: str = BUCKET) -> str:
    """THE DETACHED RESULTS ROOT: every shard of one arm at one engine commit writes under
    <bucket>/eval/<arm>-<commit12>/<kind>/shard-I-of-N/, so collect.py needs GCS and nothing else."""
    return f"{bucket}/eval/{arm}-{commit[:12]}"
