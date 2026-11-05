# Responsibility: Keep the storage stage on the artifacts bucket the deployment target pins.
# Owns: the assertions over create-object-storage.sh's bucket choice.
# Boundaries: runs only the bucket-choice lines of the script in a local bash; calls no cloud.
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
OBJECT_STORAGE = REPO / "deploy" / "gcp" / "scripts" / "create-object-storage.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _bucket_block() -> str:
    """The lines that choose ARTIFACTS_BUCKET, through the guard that follows them."""
    text = OBJECT_STORAGE.read_text(encoding="utf-8")
    start = text.index('ARTIFACTS_BUCKET="${GCP_ARTIFACTS_BUCKET')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


def _choose(env: dict[str, str]) -> subprocess.CompletedProcess:
    script = ('die() { echo "DIE: $*" >&2; exit 3; }\n' + _bucket_block()
              + 'echo "${ARTIFACTS_BUCKET}"\n')
    base = {"PATH": "/usr/bin:/bin", "DEPLOYMENT_ID": "dev", "GCP_PROJECT_NUMBER": "224734058693"}
    return subprocess.run(["bash", "-c", script], env={**base, **env},
                          capture_output=True, text=True, check=False)


def test_a_pinned_minio_bucket_is_the_bucket_the_stage_uses():
    """THE BUG THIS FILE EXISTS FOR.

    Shared dev pins MINIO_BUCKET=hexera-dev-artifacts-224734058693. The stage read only
    GCP_ARTIFACTS_BUCKET, fell through to dev-artifacts-224734058693, and wrote that back as
    MINIO_BUCKET. Each full deploy moved the API and workers to the wrong bucket; each merge deploy
    moved the API back. Old runs' viewer data then read as ObjectNotFound -> "surface 503".
    """
    res = _choose({"MINIO_BUCKET": "hexera-dev-artifacts-224734058693"})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "hexera-dev-artifacts-224734058693"


def test_with_nothing_pinned_the_derived_name_still_applies():
    res = _choose({})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "dev-artifacts-224734058693"


def test_two_pins_that_agree_are_fine():
    res = _choose({"GCP_ARTIFACTS_BUCKET": "b-1", "MINIO_BUCKET": "b-1"})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "b-1"


def test_two_pins_that_disagree_stop_the_stage():
    """Two names for one bucket that differ means the API and the workers would split."""
    res = _choose({"GCP_ARTIFACTS_BUCKET": "b-1", "MINIO_BUCKET": "b-2"})
    assert res.returncode == 3
    assert "must agree" in res.stderr
