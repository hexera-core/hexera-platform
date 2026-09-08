# Responsibility: Verify 'console' is a selectable deploy component and its stage states its skip.
# Boundaries: it reads the driver; it runs no deploy.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"


def test_console_is_a_known_component():
    text = DEPLOY.read_text(encoding="utf-8")
    known = [line for line in text.splitlines() if line.strip().startswith("_known=")]
    assert known, "the component roster moved; this test cannot find it"
    assert "console" in known[0], (
        "'console' is not a known component, so DEPLOY_COMPONENTS=console would be refused "
        "as a typo")


def test_the_console_stage_runs_the_console_script():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "create-console-service.sh" in text


def test_an_unselected_console_states_its_skip():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "skipped console" in text, (
        "a stage that is not selected must say so; silence reads as success")


def test_the_console_rolls_out_after_the_api():
    text = DEPLOY.read_text(encoding="utf-8")
    api_at = text.index("create-api-service.sh")
    console_at = text.index("create-console-service.sh")
    assert api_at < console_at, (
        "the console must roll out after the API it talks to, or its first requests 503")


DEPLOY_WF = REPO / ".github" / "workflows" / "deploy.yml"


def test_a_merge_reconciles_the_console():
    text = DEPLOY_WF.read_text(encoding="utf-8")
    assert "components=images,migrate,console" in text, (
        "a merge to main deploys the app but not the console, so the console would silently "
        "stay on an older digest after every merge")


def test_a_release_tag_still_reconciles_everything():
    text = DEPLOY_WF.read_text(encoding="utf-8")
    assert "components=all" in text
