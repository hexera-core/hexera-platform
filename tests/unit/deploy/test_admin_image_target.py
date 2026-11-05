# Responsibility: Verify the admin image target is pinned, unprivileged and serves on Cloud Run's port.
# Boundaries: it reads the Dockerfile; it builds nothing.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
DOCKERFILE = REPO / "Dockerfile"
NEXT_CONFIG = REPO / "apps" / "admin-console" / "next.config.ts"

# The console stages pin this; the admin stages must pin the SAME one. Two Node bases in one
# Dockerfile is two things to keep current, and a silent drift between them.
NODE_DIGEST = "sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e"


def _stage(name: str) -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"^FROM ", text, flags=re.M)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:end]
        if re.match(rf"^FROM \S+ AS {re.escape(name)}\s*$", block.splitlines()[0]):
            return block
    raise AssertionError(f"no Dockerfile stage named {name!r}")


def test_admin_stages_pin_the_same_node_digest_as_the_console():
    for name in ("admin-build", "admin"):
        first = _stage(name).splitlines()[0]
        assert NODE_DIGEST in first, (
            f"stage {name} does not pin {NODE_DIGEST}: {first!r}. Both Node images in this "
            f"Dockerfile must be the same base or they drift apart silently.")


def test_admin_runs_unprivileged():
    assert re.search(r"^USER (?!root)", _stage("admin"), flags=re.M)


def test_admin_honours_the_cloud_run_port():
    block = _stage("admin")
    assert "PORT=8080" in block
    assert "HOSTNAME=0.0.0.0" in block, (
        "Next's standalone server binds localhost without this, so the container starts, answers "
        "nothing, and the revision never becomes ready")
    assert 'CMD ["node", "apps/admin-console/server.js"]' in block


def test_next_emits_standalone_output_rooted_at_the_workspace():
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    assert 'output: "standalone"' in text
    assert "outputFileTracingRoot" in text, (
        "in a pnpm workspace Next traces from the app directory unless told otherwise, and the "
        "standalone bundle then omits the workspace packages the app imports")
