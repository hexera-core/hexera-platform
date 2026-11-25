#!/usr/bin/env python3
"""Read an image's org.opencontainers.image.revision label from the registry (no Docker): index -> linux/amd64
manifest -> config blob -> labels. Prints the revision; exit 1 if it is missing or differs from --expect.

    image_label.py <host/project/repo/name@sha256:...> --token "$(gcloud auth print-access-token)" [--expect SHA]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))
LABEL = "org.opencontainers.image.revision"


def get(url: str, token: str, accept: str = ACCEPT) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("image")
    p.add_argument("--token", required=True)
    p.add_argument("--expect", default="")
    a = p.parse_args()
    if "@sha256:" not in a.image:
        print(f"REFUSED: {a.image} is not pinned by digest")
        return 1
    ref, digest = a.image.split("@", 1)
    host, path = ref.split("/", 1)
    base = f"https://{host}/v2/{path}"
    m = get(f"{base}/manifests/{digest}", a.token)
    if "manifests" in m:  # an index: the linux/amd64 image
        amd = [x for x in m["manifests"] if (x.get("platform") or {}).get("architecture") == "amd64"
               and (x.get("platform") or {}).get("os") == "linux"]
        if len(amd) != 1:
            print(f"REFUSED: {len(amd)} linux/amd64 manifests in the index")
            return 1
        m = get(f"{base}/manifests/{amd[0]['digest']}", a.token)
    cfg = get(f"{base}/blobs/{m['config']['digest']}", a.token, accept="*/*")
    rev = ((cfg.get("config") or {}).get("Labels") or {}).get(LABEL)
    print(rev or "")
    if not rev:
        print(f"REFUSED: no {LABEL} label", file=sys.stderr)
        return 1
    if a.expect and rev != a.expect:
        print(f"REFUSED: label {rev} != --image-commit {a.expect}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
