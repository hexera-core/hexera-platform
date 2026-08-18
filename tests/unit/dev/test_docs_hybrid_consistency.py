# Responsibility: Verify the documentation describes one topology and never claims the product runs fully offline.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
README = (REPO / "README.md").read_text()
DEV = (REPO / "docs/development/overview.md").read_text()
ARCH = (REPO / "docs/architecture/overview.md").read_text()


def test_readme_states_the_hybrid_topology():
    # The README says WHAT the topology is; it no longer carries the commands. Requiring them here
    # kept a second quickstart in a file that only points at the setup guide, and two quickstarts
    # drift. The commands are asserted below, in the document that owns them.
    assert "hybrid" in README.lower()
    assert "Cloud Run" in README and "mesh" in README.lower()
    assert "docs/getting-started/setup.md" in README, \
        "the README must point at the document that installs it"


def test_the_setup_guide_carries_the_commands_that_install_and_start_it():
    setup = (REPO / "docs/getting-started/setup.md").read_text()
    assert "make setup" in setup and "make dev-up" in setup


def test_dev_guide_states_compose_is_a_control_plane_not_offline():
    low = DEV.lower()
    assert "control plane" in low
    assert "not a fully offline" in low or "not a fully-offline" in low
    # coworkers do NOT install the native toolchain for the app (allow markdown emphasis on "not")
    import re as _re
    plain = _re.sub(r"[*_`]", "", low)
    assert "do not install" in plain and ("openfoam" in plain and "vmtk" in plain)


def test_dev_guide_marks_native_local_as_test_only():
    low = DEV.lower()
    assert "test-native" in low
    assert "not the normal developer workflow" in low or "specialist" in low


def test_architecture_states_one_topology_and_one_mesh_boundary():
    low = ARCH.lower()
    for term in ("one application topology", "one mesh boundary", "native tests"):
        assert term in low, f"architecture.md must state {term!r}"
    assert "no selector and no fallback" in low, (
        "architecture.md must say the application cannot redirect meshing")


def test_no_doc_claims_the_product_is_fully_offline_via_compose():
    for name, text in (("README.md", README), ("docs/development/overview.md", DEV)):
        low = text.lower()
        # a claim like "runs entirely offline"/"fully offline product" would be wrong (mesh is cloud)
        assert "runs entirely offline" not in low, f"{name} wrongly implies a fully-offline product"
