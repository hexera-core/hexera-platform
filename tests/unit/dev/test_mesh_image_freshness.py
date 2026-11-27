# Responsibility: Verify the native runners refuse a mesh image not built from the current tree.
# Boundaries: the freshness contract - whether a native run then passes is the native tier's own proof.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MAKEFILE = (REPO / "Makefile").read_text()
VALIDATE = REPO / "devtools" / "release" / "validate.sh"
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


def _logical_lines(body: str) -> list[str]:
    """The script's commands with backslash continuations joined, so a flag on the second line of
    a command belongs to that command and prose near it does not."""
    out, cur = [], ""
    for raw in body.splitlines():
        cur += raw.rstrip()
        if cur.endswith("\\"):
            cur = cur[:-1] + " "
            continue
        out.append(cur)
        cur = ""
    if cur:
        out.append(cur)
    return out


def test_the_release_build_stamps_it_too():
    """THE GATE THAT PUBLISHES THE IMAGE HAS TO STAMP IT, AND IT DID NOT.

    `make mesh-image` passes the build-arg (the test above), but Gate C builds its OWN images and
    devtools/release/validate.sh's `_build_image` passed only APP_VERSION - so Dockerfile's
    `ARG MESH_SOURCE_TREE=unknown` default stood on everything it published.

    MEASURED, 2026-11-27: the mesh image behind the Cloud Run job dev-rehaan-mesh
    (mesh@sha256:8788eec1dd..., published by this gate from commit 8a22538f) reported
    MESH_SOURCE_TREE=unknown when its config blob was read out of the registry. Together with
    test_the_freshness_check_refuses_empty_unknown_and_mismatched_stamps below that is the whole
    defect: the freshness gate every native tier runs FIRST refuses "unknown", so the one check
    that proves an image is the checkout under test could not be satisfied by any released image.

    BOTH BUILD PATHS, because they are the same build with and without a layer cache - a stamp on
    only one would make identifiability depend on whether a buildx builder happened to come up.

    THE ASSERTION IS ON THE COMMAND, NOT ON NEARBY TEXT. An earlier version of this test searched
    the lines around each build and passed with the build-arg deleted, because it matched the word
    in a comment. Continuations are joined first so only the real flag counts.
    """
    builds = [ln for ln in _logical_lines(VALIDATE.read_text())
              if ln.strip().startswith(("docker build ", "docker buildx build "))]
    assert builds, "no image build found in the release gate; this test reads the wrong file"
    for ln in builds:
        assert f"--build-arg {BUILD_ARG}=" in ln, (
            f"a release build does not stamp {BUILD_ARG}, so the image it publishes cannot be "
            f"identified: {ln.strip()[:90]}")


def test_the_release_build_takes_its_digest_from_the_one_authority():
    """Not a second implementation. The Makefile stamps $(SOURCE_DIGEST), which is
    tests/integration/source_digest.sh, and assert_mesh_image_current.sh recomputes that same
    script to compare against - so a digest computed any other way here would make a correctly
    built image look stale.

    Only the STAMP's provenance is pinned. validate.sh hashes other things for its own reasons,
    so a blanket ban on hashing tools would be a false positive.
    """
    body = VALIDATE.read_text()
    assert "tests/integration/source_digest.sh" in body,         "the release gate computes the image stamp some other way"
    stamp = next((ln for ln in _logical_lines(body)
                  if ln.strip().startswith("SOURCE_TREE=")), "")
    assert "source_digest.sh" in stamp,         f"the stamp is not taken from the one digest authority: {stamp.strip()[:90]}"


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
