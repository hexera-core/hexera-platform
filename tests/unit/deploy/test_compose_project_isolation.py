# Responsibility: Prove the TRACKED compose file scopes its network and volumes to the project.
# Boundaries: rendering only - what two live projects then resolve over real DNS is the integration tier.
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from tests._compose_support import product_version, render

REPO = Path(__file__).resolve().parents[3]

# Rendering needs the Compose CLI, not a running daemon: `config` interpolates and validates the
# file without contacting dockerd. This tier starts nothing and pulls nothing.
pytestmark = pytest.mark.skipif(shutil.which("docker") is None,
                                reason="the docker CLI is not installed here")


def _rendered(project: str) -> dict:
    # Compose's own rendering of the TRACKED file, under a project name of our choosing - the same
    # thing `docker compose up` would resolve, asked without starting anything. The render itself
    # is hermetic and owned by tests/_compose_support.py.
    import yaml

    out = render(project=project, env={"APP_VERSION": product_version()})
    assert out.returncode == 0, f"the tracked compose file did not render:\n{out.stderr}"
    return yaml.safe_load(out.stdout)


# 1-3. the tracked compose file, as Compose actually renders it

def test_the_tracked_compose_file_names_no_global_network():
    net = _rendered("renderprobe").get("networks") or {}
    fixed = {k: v.get("name") for k, v in net.items() if isinstance(v, dict) and v.get("name")}
    for key, name in fixed.items():
        assert name.startswith("renderprobe"), (
            f"network '{key}' is pinned to the global name '{name}'. Every project that declares "
            "it lands on ONE network, where `postgres` no longer means this project's database.")


def test_two_projects_render_different_network_names():
    one = (_rendered("isoalpha").get("networks") or {}).get("default", {}).get("name")
    two = (_rendered("isobravo").get("networks") or {}).get("default", {}).get("name")
    assert one and two and one != two, (
        f"two projects would share the network '{one}' - the collision is in the file itself")


def test_volumes_are_scoped_the_same_way():
    # The volumes were always project-scoped; the network has to match them or the stack is split
    # between private storage and shared naming.
    vols = _rendered("isoalpha").get("volumes") or {}
    named = {k: v.get("name") for k, v in vols.items() if isinstance(v, dict) and v.get("name")}
    stray = {k: n for k, n in named.items() if not n.startswith("isoalpha")}
    assert stray == {}, f"these volumes are global, not project-scoped: {stray}"


# 4. nothing in the tree assumes the old literal any more

def test_no_tracked_file_attaches_to_the_old_fixed_network():
    # The migration note that tells an older stack how to remove its leftover network is the one
    # legitimate mention. It lives in the architecture overview; naming the network in order to
    # delete it is the opposite of attaching to it.
    notes = ("docs/architecture/overview.md", "docs/getting-started/setup.md")
    proc = subprocess.run(["git", "grep", "-n", "mesh-network", "--", ".",
                           *(f":(exclude){n}" for n in notes), ":(exclude)AUDIT-*.md",
                           ":(exclude)tests/unit/deploy/test_compose_project_isolation.py",
                           ":(exclude)tests/integration/test_compose_network_isolation_live.py"],
                          cwd=str(REPO), capture_output=True, text=True)
    assert proc.returncode in (0, 1), f"the search itself failed: {proc.stderr[:200]}"
    assert proc.stdout.strip() == "", (
        "something still names the fixed network. Only the migration notes in "
        f"{' and '.join(notes)} may:\n" + proc.stdout)
