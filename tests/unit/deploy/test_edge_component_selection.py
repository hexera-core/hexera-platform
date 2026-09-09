# Responsibility: Verify 'edge' is selectable, ordered last, and its address is recorded.
# Boundaries: it reads the driver and the state writer; it runs no deploy.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_edge_is_a_known_component():
    known = [l for l in DEPLOY.read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("_known=")]
    assert known and "edge" in known[0]


def test_the_edge_stage_runs_the_edge_script():
    assert "create-edge.sh" in DEPLOY.read_text(encoding="utf-8")


def test_an_unselected_edge_states_its_skip():
    assert "skipped edge" in DEPLOY.read_text(encoding="utf-8")


def test_the_edge_runs_after_the_services_it_fronts():
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("create-console-service.sh") < text.index("create-edge.sh")
    assert text.index("create-admin-service.sh") < text.index("create-edge.sh")


def test_the_reserved_address_is_recorded():
    text = WRITER.read_text(encoding="utf-8")
    assert '"edge"' in text, (
        "the manifest records no edge, so the address an operator put in DNS survives only in the "
        "log of the run that printed it")
    assert "EDGE_IP_ADDRESS" in text
