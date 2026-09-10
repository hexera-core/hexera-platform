# Responsibility: Verify the state manifest names the console service and its digest.
# Boundaries: it reads the writer's declarations; it calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_the_console_service_is_a_recorded_resource():
    text = WRITER.read_text(encoding="utf-8")
    assert '"console_service"' in text, (
        "the manifest records no console_service, so mesh-destroy.sh could not tell a "
        "tooling-created console from one somebody made by hand")


def test_the_console_digest_is_read_back_for_an_unselected_run():
    text = WRITER.read_text(encoding="utf-8")
    assert "CONSOLE_DIGEST" in text, (
        "a run that did not select 'console' must still record the digest the live service "
        "carries, not an empty value")


def test_the_console_disposition_is_component_aware():
    text = WRITER.read_text(encoding="utf-8")
    assert "_disp console" in text, (
        "the console's disposition must distinguish reconciled from not selected, like every "
        "other resource here")
