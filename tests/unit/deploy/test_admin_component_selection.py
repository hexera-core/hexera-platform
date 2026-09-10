# Responsibility: Verify 'admin' is a selectable component, ordered after the API, and recorded.
# Boundaries: it reads the driver and the state writer; it runs no deploy.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_admin_is_a_known_component():
    known = [l for l in DEPLOY.read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("_known=")]
    assert known and "admin" in known[0], (
        "'admin' is not a known component, so DEPLOY_COMPONENTS=admin would be refused as a typo")


def test_the_admin_stage_runs_the_admin_script():
    assert "create-admin-service.sh" in DEPLOY.read_text(encoding="utf-8")


def test_an_unselected_admin_states_its_skip():
    assert "skipped admin" in DEPLOY.read_text(encoding="utf-8")


def test_the_admin_rolls_out_after_the_api():
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("create-api-service.sh") < text.index("create-admin-service.sh")


def test_the_admin_service_is_a_recorded_resource():
    text = WRITER.read_text(encoding="utf-8")
    assert '"admin_service"' in text
    assert "ADMIN_DIGEST" in text, (
        "a run that did not select 'admin' must still record the digest the live service carries")
    assert "_disp admin" in text
