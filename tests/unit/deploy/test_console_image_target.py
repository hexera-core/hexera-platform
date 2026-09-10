# Responsibility: Verify the console image target is pinned, unprivileged and serves on the port Cloud Run sets.
# Boundaries: it reads the Dockerfile; it builds nothing.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
DOCKERFILE = REPO / "Dockerfile"
NEXT_CONFIG = REPO / "apps" / "console" / "next.config.ts"


def _stage(name: str) -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"^FROM ", text, flags=re.M)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:end]
        if re.match(rf"^FROM \S+ AS {re.escape(name)}\s*$", block.splitlines()[0]):
            return block
    raise AssertionError(f"no Dockerfile stage named {name!r}")


def test_console_stages_pin_their_base_by_digest():
    for name in ("console-build", "console"):
        first = _stage(name).splitlines()[0]
        assert "@sha256:" in first, (
            f"stage {name} does not pin its base by digest: {first!r}. Every other FROM in "
            f"this file is pinned, and an unpinned base makes the build unreproducible.")


def test_console_runs_unprivileged():
    block = _stage("console")
    assert re.search(r"^USER (?!root)", block, flags=re.M), (
        "the console stage does not drop to a non-root user")


def test_console_honours_the_cloud_run_port():
    block = _stage("console")
    assert "PORT=8080" in block, "the console stage does not default PORT"
    assert "HOSTNAME=0.0.0.0" in block, (
        "Next's standalone server binds localhost unless HOSTNAME is set, which on Cloud Run "
        "means the container answers nothing and the revision never becomes ready")
    assert 'CMD ["node", "apps/console/server.js"]' in block


def test_next_emits_standalone_output_rooted_at_the_workspace():
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'output: "standalone"' in text
    assert "outputFileTracingRoot" in text, (
        "in a pnpm workspace Next traces from the app directory unless told otherwise, and the "
        "standalone bundle then omits the workspace packages the console imports")
