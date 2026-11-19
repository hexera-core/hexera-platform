# Responsibility: Verify a service rollout prunes the revisions nothing routes to, and only those.
# Boundaries: drives lib.sh's prune_idle_revisions against a fake gcloud and reads what it deleted.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
LIB = REPO / "deploy" / "gcp" / "scripts" / "lib.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
ARGS="$*"
case "$ARGS" in
  *"run services describe"*) [ -n "${FAKE_LIVE:-}" ] || exit 1; printf '%s\n' "${FAKE_LIVE}" ;;
  *"run revisions list"*)    printf '%s\n' ${FAKE_REVISIONS} ;;
  *"run revisions delete"*)  printf '%s\n' "$ARGS" >> "${FAKE_DELETES:?}" ;;
esac
"""


def _prune(tmp_path: Path, *, live: str, revisions: list[str], keep: str | None = None) -> list[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcloud = bindir / "gcloud"
    gcloud.write_text(_FAKE_GCLOUD, encoding="utf-8")
    gcloud.chmod(0o755)
    deletes = tmp_path / "deletes.log"
    deletes.touch()
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GCP_PROJECT_ID": "fake-proj",
        "GCP_REGION": "us-central1",
        "FAKE_LIVE": live,
        "FAKE_REVISIONS": " ".join(revisions),
        "FAKE_DELETES": str(deletes),
    }
    if keep is not None:
        env["REVISION_KEEP"] = keep
    subprocess.run(["bash", "-c", f'set -euo pipefail; source "{LIB}"; prune_idle_revisions svc'],
                   env=env, check=True, capture_output=True, text=True)
    return sorted(line.split("delete ")[1].split()[0] for line in deletes.read_text().splitlines())


def test_idle_revisions_beyond_the_newest_three_are_deleted(tmp_path):
    revisions = [f"svc-{i:05d}" for i in range(8, 0, -1)]  # newest first, as listed
    assert _prune(tmp_path, live="svc-00008", revisions=revisions) == [f"svc-{i:05d}" for i in range(1, 6)]


def test_a_revision_with_traffic_or_a_tag_is_kept_however_old(tmp_path):
    revisions = [f"svc-{i:05d}" for i in range(6, 0, -1)]
    deleted = _prune(tmp_path, live="svc-00006;svc-00001", revisions=revisions)
    assert deleted == ["svc-00002", "svc-00003"]


def test_nothing_is_deleted_when_the_live_traffic_cannot_be_read(tmp_path):
    revisions = [f"svc-{i:05d}" for i in range(6, 0, -1)]
    assert _prune(tmp_path, live="", revisions=revisions) == []


def test_the_rollback_window_is_configurable(tmp_path):
    revisions = [f"svc-{i:05d}" for i in range(4, 0, -1)]
    assert _prune(tmp_path, live="svc-00004", revisions=revisions, keep="1") == ["svc-00001", "svc-00002", "svc-00003"]
