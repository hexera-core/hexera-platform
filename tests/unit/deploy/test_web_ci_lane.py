# Responsibility: Verify the pnpm workspace is checked by CI and counted by the aggregate gate.
# Boundaries: it reads the workflow document; it runs no build and reaches no network.
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
CI = REPO / ".github" / "workflows" / "ci.yml"


def _doc() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_preflight_publishes_a_web_scope_output():
    jobs = _doc()["jobs"]
    outputs = jobs["preflight"]["outputs"]
    assert "run_web" in outputs, (
        "the scope step must publish run_web, so a documentation-only change skips the "
        "workspace lane exactly as it skips the python and image lanes")


def test_web_lane_exists_and_is_scope_gated():
    web = _doc()["jobs"]["web"]
    assert web["if"] == "needs.preflight.outputs.run_web == 'true'"
    steps = yaml.dump(web["steps"])
    for expected in ("pnpm install --frozen-lockfile", "pnpm typecheck",
                     "pnpm lint", "pnpm test", "pnpm build"):
        assert expected in steps, f"the web lane does not run {expected!r}"


def test_web_lane_is_counted_by_the_gate():
    gate = _doc()["jobs"]["ci-gate"]
    assert "web" in gate["needs"], "ci-gate does not wait for the web lane"
    body = yaml.dump(gate["steps"])
    assert "web=${{ needs.web.result }}" in body, (
        "ci-gate waits for the web lane but never reads its result, so a failing "
        "workspace would still post a green required check")
