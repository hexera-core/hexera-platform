# Responsibility: Verify the native runners refuse a mesh image not built from the current tree.
# Boundaries: the freshness contract - whether a native run then passes is the native tier's own proof.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MAKEFILE = (REPO / "Makefile").read_text()
ASSERT = REPO / "tests" / "native" / "assert_mesh_image_current.sh"
RUNNERS = (REPO / "tests" / "native" / "run_tier.sh",
           REPO / "tests" / "native" / "run_terminal_matrix.sh")
BUILD_ARG = "MESH_SOURCE_TREE"


def _recipe(target: str) -> str:
    body = MAKEFILE[MAKEFILE.index(f"\n{target}:") + 1:]
    rest = re.search(r"\n(?=[A-Za-z0-9_.-]+:)", body)
    return body[:rest.start()] if rest else body


def test_the_mesh_build_stamps_the_working_tree_digest():
    recipe = _recipe("mesh-image")
    assert BUILD_ARG in recipe, "the mesh build no longer stamps the source digest"
    assert "SOURCE_DIGEST" in recipe, "the mesh build no longer computes the digest"


def test_the_freshness_check_uses_the_one_digest_authority():
    body = ASSERT.read_text()
    assert "tests/integration/source_digest.sh" in body, \
        "the mesh freshness check computes a digest of its own"
    for algo in ("sha256sum", "hash-object", "rev-parse HEAD"):
        assert algo not in body, f"{algo!r} is a second digest implementation"


def test_the_freshness_check_refuses_empty_unknown_and_mismatched_stamps():
    body = ASSERT.read_text()
    assert '-z "$GOT"' in body, "an unstamped image is no longer refused"
    assert '"$GOT" = "unknown"' in body, "an 'unknown' stamp is no longer refused"
    assert '"$GOT" != "$WANT"' in body, "a mismatched stamp is no longer refused"
    assert "make mesh-image" in body, "the refusal no longer names the real rebuild target"


def test_the_freshness_check_never_builds_or_pulls():
    body = ASSERT.read_text()
    for forbidden in ("docker build", "docker pull", "docker run", "docker compose"):
        assert forbidden not in body, f"the freshness check runs {forbidden!r}"


def test_no_native_runner_builds_its_own_image():
    for runner in RUNNERS:
        body = runner.read_text()
        assert "docker build" not in body, (
            f"{runner.name} builds the mesh image itself; a runner that does that can turn a "
            "stale image current inside its own preflight")
        assert "assert_mesh_image_current.sh" in body, \
            f"{runner.name} no longer verifies image freshness before native execution"


def test_the_freshness_check_runs_before_any_native_container():
    for runner in RUNNERS:
        body = runner.read_text()
        check = body.index("assert_mesh_image_current.sh")
        runs = [m.start() for m in re.finditer(r"docker run", body)]
        assert all(pos > check for pos in runs), \
            f"{runner.name} starts a container before verifying the image"
