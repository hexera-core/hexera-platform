# Responsibility: Print the settings a successful provision used, for the developer to confirm against .env.
# Boundaries: reads the deployment record write-deployment-state.sh owns; it prints resource names and writes nothing.
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / "deploy" / "output" / "deployment.json"


def settings() -> dict[str, str]:
    doc = json.loads(STATE.read_text())
    res = doc.get("resources", {})
    return {
        "GCP_PROJECT_ID": doc.get("project", ""),
        "GCP_REGION": doc.get("region", ""),
        "CLOUDRUN_JOB": (res.get("mesh_job") or {}).get("name", ""),
        "GCP_MESH_BUCKET": (res.get("exchange_bucket") or {}).get("name", ""),
    }


def main() -> int:
    if not STATE.exists():
        sys.stderr.write(f"no deployment record at {STATE} - provisioning did not complete\n")
        return 1
    values = settings()
    if missing := [k for k, v in values.items() if not v]:
        sys.stderr.write(f"the deployment record names no {', '.join(missing)}\n")
        return 1

    # Names of resources, never a credential: the identity the stack meshes as is the file
    # GOOGLE_ADC_FILE points at, which provisioning neither reads nor produces.
    print()
    # Provisioning reads these names FROM .env and creates what they name, so telling the operator
    # to copy them back described an edit that is never needed. They are printed to be compared.
    print("  Provisioned. These are the names provisioning used. Confirm .env matches:")
    print()
    for key, value in values.items():
        print(f"      {key}={value}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
