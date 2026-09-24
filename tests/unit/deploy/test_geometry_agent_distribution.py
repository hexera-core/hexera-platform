# Responsibility: Prove the built image installs the geometry agent, and that what it installs is not stale.
# Boundaries: reads the repository's own build inputs; it builds no image and imports no agent module, so it
#             runs and REPORTS in every environment - including the one where the package is absent, which is
#             the environment the defect it guards hid in.
#
# THE DEFECT THIS EXISTS FOR. Nothing installed the geometry-agent distribution into any image: not
# requirements/runtime.txt, not requirements/dev.txt, not pyproject.toml (whose packages.find reads
# where=["src"], and the platform src holds only meshpipeline), not a Dockerfile line, not a deploy script.
# docker-compose.yml said so outright. Every entry point wraps its import in try/except and treats absence
# as an outcome to record, so nothing raised: no document, no survey, no plan, and a planner message byte
# for byte identical to the pre-Surveyor one.
#
# AND IT WAS INVISIBLE TO EVERY CHECK, because the tests that covered the Surveyor were
# pytest.importorskip("geometry_agent...") and skipped themselves in exactly the image where the package
# was missing. So every assertion below is made from FILES, never from an import: this test cannot be
# silenced by the absence of the thing it is about.
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VENDOR = REPO / "vendor" / "wheels"
PROVENANCE = VENDOR / "PROVENANCE.json"
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")


def _wheels() -> list[Path]:
    return sorted(VENDOR.glob("hexera_geometry_agent-*.whl"))


def _the_wheel() -> Path:
    found = _wheels()
    assert len(found) == 1, (
        f"expected exactly one vendored geometry-agent wheel in {VENDOR.relative_to(REPO)}, found "
        f"{[p.name for p in found]}. The Dockerfile's wheel stage asserts the same thing and would fail "
        f"the build; build one with `bash deploy/vendor_geometry_agent.sh /path/to/geometry_agent`.")
    return found[0]


def _imported_subpackages() -> set[str]:
    """Every `geometry_agent.<subpackage>` the application imports, read out of src/ rather than listed.

    A hand-kept list is the same blind spot again: it would say the wheel is complete on the day a new
    import is added. This derives the answer from the imports themselves.
    """
    found: set[str] = set()
    # IMPORT STATEMENTS ONLY, not every mention of the name: two schema string constants read
    # "geometry_agent.measurement.v1" and "geometry_agent.planner_block.v1", and neither is a module.
    # `from geometry_agent import hexera`-style imports are covered by the second pattern.
    dotted = re.compile(r"^\s*(?:from|import)\s+geometry_agent\.([A-Za-z_][A-Za-z0-9_]*)", re.M)
    bare = re.compile(r"^\s*from\s+geometry_agent\s+import\s+([A-Za-z_][A-Za-z0-9_, ]*)", re.M)
    dynamic = re.compile(r"import_module\(\s*[\"']geometry_agent\.([A-Za-z_][A-Za-z0-9_]*)")
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(dotted.findall(text))
        found |= set(dynamic.findall(text))
        for group in bare.findall(text):
            found |= {name.strip() for name in group.split(",") if name.strip()}
    assert found, "no geometry_agent import found in src/ at all - this test is checking nothing"
    return found


# ---------------------------------------------------------------- the image installs it

def test_the_wheel_stage_collects_the_vendored_geometry_wheel_into_dist():
    # /dist is what both runtime blocks install from, so collecting it here is what puts the package in
    # the api, pipeline AND mesh images without a fourth place to keep in step.
    assert "COPY vendor/wheels/ /vendor/wheels/" in DOCKERFILE, (
        "the Dockerfile's `wheel` stage does not copy vendor/wheels/ into the build")
    assert "cp /vendor/wheels/hexera_geometry_agent-*.whl /dist/" in DOCKERFILE, (
        "the vendored geometry wheel is copied nowhere - /dist is what the runtime stages install from")


def test_the_build_refuses_to_proceed_without_exactly_one_geometry_wheel():
    # The whole failure mode was an image that quietly lacked the package. A build that finds no wheel,
    # or two, must stop rather than produce one.
    assert 'if [ "$count" != "1" ]' in DOCKERFILE and "exit 1" in DOCKERFILE, (
        "the Dockerfile does not assert exactly one vendored geometry wheel before copying it")


def test_every_runtime_target_installs_everything_in_dist():
    # `pip install --no-deps /tmp/*.whl` is the line that installs BOTH distributions. There are two of
    # these blocks - runtime-base (api, pipeline) and mesh - and both must be glob installs, because a
    # block that named meshpipeline's wheel explicitly would silently skip the geometry one.
    installs = DOCKERFILE.count("pip install --no-cache-dir --no-deps /tmp/*.whl")
    assert installs == 2, (
        f"expected the two runtime blocks (runtime-base and mesh) to install every wheel in /tmp, found "
        f"{installs} such lines - a stage that does not is a target with no Surveyor")


def test_the_build_verifies_the_geometry_agent_resolves_before_the_image_is_finished():
    verify = (REPO / "deploy" / "verify_install.py").read_text(encoding="utf-8")
    assert "import geometry_agent" in verify, (
        "deploy/verify_install.py does not import the geometry agent, so a stage that failed to install "
        "it still builds - which is exactly how the defect survived")
    assert "GEOMETRY_SUBPACKAGES" in verify and "GEOMETRY_DATA" in verify, (
        "deploy/verify_install.py checks that the agent imports but not that it is COMPLETE; a stale "
        "wheel imports fine and fails at a customer's upload")
    assert DOCKERFILE.count("python /tmp/verify_install.py") == 2, (
        "verify_install.py does not run in both runtime blocks")


def test_the_environment_the_wheel_installs_into_carries_what_the_agent_imports():
    # The wheel is installed with --no-deps, the same as meshpipeline's, so requirements/runtime.txt is
    # what has to carry its imports. trimesh was the only one missing.
    runtime = (REPO / "requirements" / "runtime.txt").read_text(encoding="utf-8")
    assert re.search(r"^trimesh==", runtime, re.M), (
        "requirements/runtime.txt does not pin trimesh, which geometry_agent.facts imports at module "
        "level - the wheel installs but the first measurement raises ImportError")


# ---------------------------------------------------------------- what it installs is not stale

def test_the_vendored_wheel_matches_its_recorded_provenance():
    wheel = _the_wheel()
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert doc["wheel"] == wheel.name, (
        f"PROVENANCE.json describes {doc['wheel']} but the vendored wheel is {wheel.name} - one of them "
        f"was replaced without the other")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert digest == doc["sha256"], (
        "the vendored wheel's bytes do not match the sha256 PROVENANCE.json records, so nothing here "
        "describes the wheel the image would install. Rebuild it with deploy/vendor_geometry_agent.sh.")


def test_the_wheel_names_the_agent_commit_it_was_built_from():
    # A static 0.1.0 makes two wheels built six months apart indistinguishable, which is how
    # dist/hexera-geometry-agent.tar.gz went stale unnoticed. The local version segment puts the commit
    # where `importlib.metadata.version` inside a running container can read it.
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", doc["agent_commit"]), (
        f"PROVENANCE.json records no usable agent commit: {doc['agent_commit']!r}")
    assert doc["agent_commit"].startswith(doc["agent_commit_short"])
    assert f"+g{doc['agent_commit_short']}" in doc["version"], (
        f"the wheel's version {doc['version']!r} does not carry the agent commit")
    assert doc["version"] in _the_wheel().name.replace("_", "+", 0) or \
        doc["agent_commit_short"] in _the_wheel().name, (
        f"the wheel filename {_the_wheel().name} does not carry the commit its provenance records")


def test_a_release_wheel_was_not_built_from_a_dirty_agent_checkout():
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert doc["agent_checkout_dirty"] is False, (
        "the vendored wheel was built from an agent checkout with uncommitted changes, so the commit it "
        "records does not describe it. Rebuild from a clean checkout.")


def test_the_vendored_wheel_carries_every_subpackage_the_application_imports():
    # THE staleness test. The abandoned dist/hexera-geometry-agent.tar.gz carried agent, cli, evaluate,
    # facts, render, synthetic and testing - and none of ask, chain, contract, learn, reconcile or
    # vision, every one of which src/meshpipeline imports today. A wheel like that installs without
    # complaint and fails at the first real job.
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    present = {n.split("/")[1] for n in names if n.startswith("geometry_agent/") and n.count("/") > 1}
    present |= {n.split("/")[1].removesuffix(".py") for n in names
                if n.startswith("geometry_agent/") and n.endswith(".py") and n.count("/") == 1}
    missing = sorted(_imported_subpackages() - present)
    assert not missing, (
        f"src/meshpipeline imports geometry_agent.{{{', '.join(missing)}}}, and the vendored wheel does "
        f"not carry {'them' if len(missing) > 1 else 'it'}. The wheel is stale - rebuild it from an "
        f"agent checkout that has them.")


def test_the_build_time_subpackage_gate_covers_what_the_application_actually_imports():
    # deploy/verify_install.py's list is what FAILS THE BUILD, so a list that has drifted below the real
    # imports is a gate that passes an incomplete wheel. Parsed textually because that file is
    # deliberately stdlib-only and is not importable from here without a built meshpipeline.
    verify = (REPO / "deploy" / "verify_install.py").read_text(encoding="utf-8")
    block = re.search(r"GEOMETRY_SUBPACKAGES\s*=\s*\(([^)]*)\)", verify)
    assert block, "GEOMETRY_SUBPACKAGES is not declared as a tuple literal in deploy/verify_install.py"
    gated = set(re.findall(r'"([^"]+)"', block.group(1)))
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    real_subpackages = {n.split("/")[1] for n in names
                        if n.startswith("geometry_agent/") and n.endswith("/__init__.py")}
    uncovered = sorted((_imported_subpackages() & real_subpackages) - gated)
    assert not uncovered, (
        f"the application imports geometry_agent.{{{', '.join(uncovered)}}} but the build-time gate in "
        f"deploy/verify_install.py does not check for {'them' if len(uncovered) > 1 else 'it'}, so an "
        f"image missing {'them' if len(uncovered) > 1 else 'it'} would still build")


@pytest.mark.parametrize("rel", ["geometry_agent/agent/thresholds.json",
                                 "geometry_agent/agent/identity_tests.json",
                                 "geometry_agent/learn/rules.json"])
def test_the_vendored_wheel_carries_the_data_the_package_reads(rel):
    # The agent's own pyproject declares no package-data, so a wheel built from it as-is ships .py files
    # only. identity_tests.json is read with .read_text(), so its absence is a FileNotFoundError the
    # first time a plan is built - at a customer's upload, not at build.
    # deploy/vendor_geometry_agent.sh adds the declaration; this is what proves it stayed added.
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    assert rel in names, (
        f"the vendored wheel does not carry {rel}. It was built without the package-data declaration "
        f"deploy/vendor_geometry_agent.sh adds - the installed package cannot read its own tables.")


def test_the_wheel_ships_no_test_or_eval_tree():
    # An image gets the library, not the agent's own suite or its corpus. This also catches a wheel built
    # by something other than the vendoring script, which is what would ship them.
    names = zipfile.ZipFile(_the_wheel()).namelist()
    stowaways = [n for n in names if n.startswith(("tests/", "eval/", "docs/"))]
    assert not stowaways, f"the vendored wheel ships {len(stowaways)} files it should not: {stowaways[:5]}"
