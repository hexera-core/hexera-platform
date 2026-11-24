#!/usr/bin/env python3
# Responsibility: Build the v4 toolchain (mesh) image from a CLEAN commit, push it, and read the commit back off the
#                 pushed image, so a wave's --image / --image-commit pair is a checked fact, not "built 2 minutes after".
# Owns: `build_and_push`, `Built`, the CLI.
# Boundaries: the label is stamped by `make mesh-image` (Makefile SOURCE_REVISION); the check that a launch's
#             --image-commit equals it is preflight's (`check_image_commit`), the same function used here.
"""Build, push and verify the v4 mesh image.

    image_build.py [--repo us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh]

Run it on an x86 build VM (the image is amd64 and heavy), from a checkout of the commit to build, with nothing
uncommitted: a dirty tree is refused, because an image built from uncommitted work is from no commit. It prints the
digest-pinned IMAGE and the IMAGE_COMMIT to hand wave.py / launch_fleet.py, which re-read the label and refuse a
mismatch.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight as pf  # noqa: E402

DEFAULT_REPO = "us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh"
#: A cold mesh-image build (OpenFOAM, ftetwild, mmg from source) takes hours; preflight's runner allows 120 s, which
#: is right for a registry read and refused the first real build.
BUILD_TIMEOUT_S = 4 * 3600.0


def _long_runner(argv: Sequence[str]) -> pf.Result:
    """The build and the push: same contract as preflight's runner, with the build's time limit."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return pf.Result(124, "", f"{type(exc).__name__}: {exc}")
    return pf.Result(done.returncode, done.stdout, done.stderr)


class BuildRefused(RuntimeError):
    """The image was not built, or not pushed: nothing to launch with."""


@dataclass(frozen=True)
class Built:
    image: str  # <repo>@sha256:<digest>
    image_commit: str  # the 40-hex commit read back from the pushed image's label
    link: str  # preflight's verified-link string


def build_and_push(repo: str = DEFAULT_REPO, *, runner: pf.Runner | None = None) -> Built:
    """`runner` (tests) replaces every command; by default reads use preflight's runner and the build and push use
    `_long_runner`."""
    run = runner or pf.gcloud_runner()
    heavy = runner or _long_runner
    if run(["git", "status", "--porcelain"]).out.strip():
        raise BuildRefused("the tree has uncommitted changes: commit them, an image built from them is from no commit")
    head = run(["git", "rev-parse", "HEAD"]).out.strip()
    if not pf.COMMIT_RE.match(head):
        raise BuildRefused(f"cannot read HEAD: {head!r}")
    tag = f"{repo}:{head[:12]}"
    for argv, what in ((["make", "mesh-image", f"MESH_IMAGE={tag}"], "build"), (["docker", "push", tag], "push")):
        done = heavy(argv)
        if done.rc != 0:
            raise BuildRefused(f"{what} failed: {done.err.strip()[-300:]}")
    image = pf.resolve_to_digest(tag, run)
    link = pf.check_image_commit(head, pf.image_revision(run, image))  # raises unless the label IS head
    return Built(image, head, link)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args(argv)
    try:
        built = build_and_push(args.repo)
    except (BuildRefused, ValueError, RuntimeError) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    print(f"IMAGE={built.image}\nIMAGE_COMMIT={built.image_commit}\n# {built.link}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
