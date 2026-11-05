# Responsibility: Pin the verdict-by-limits switch's path from the deploy target to the worker: on for shared dev and every personal environment, unstated for prod.
# Boundaries: static reads of the deploy workflow, create-worker-fleet.sh and startup.sh; nothing in the cloud is touched.
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_switch_follows_the_target_to_the_worker_env():
    wf = _text(".github/workflows/deploy.yml")
    fleet = _text("deploy/gcp/scripts/create-worker-fleet.sh")
    boot = _text("deploy/gcp/worker/startup.sh")
    assert "verdict_by_limits: ${{ steps.pick.outputs.verdict_by_limits }}" in wf
    assert "VERDICT_BY_LIMITS: ${{ needs.target.outputs.verdict_by_limits }}" in wf
    assert 'WORKER_METADATA+=("verdict-by-limits=${VERDICT_BY_LIMITS}")' in fleet
    assert '"verdict-by-limits:VERDICT_BY_LIMITS"' in boot


def test_dev_and_personal_environments_state_it_and_prod_does_not():
    wf = _text(".github/workflows/deploy.yml")
    # each target branch ends at its worker env object; the switch is stated right after it
    for uri in ("gs://${_did}-transfer-${_num}/worker.env", "gs://dev-transfer-224734058693/worker.env"):
        at = wf.index(f'echo "worker_env_uri={uri}"')
        assert 'echo "verdict_by_limits=true"' in wf[at:at + 400]
    prod = wf.index('echo "worker_env_uri=gs://prod-artifacts-688073002171/deploy/worker.env"')
    assert "verdict_by_limits" not in wf[prod:prod + 3000]
    assert len(re.findall(r'echo "verdict_by_limits=true"', wf)) == 2
