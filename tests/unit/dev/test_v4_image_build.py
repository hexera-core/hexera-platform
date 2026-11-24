"""v4_eval image_build: the toolchain image carries the commit it was built from, read back from the registry.
Hermetic: a scripted runner stands in for git, make, docker and gcloud."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
_SPEC = importlib.util.spec_from_file_location("v4_image_build_under_test", V4 / "image_build.py")
assert _SPEC is not None and _SPEC.loader is not None
ib = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ib  # a dataclass resolves its module through sys.modules
_SPEC.loader.exec_module(ib)
pf = ib.pf

HEAD = "a" * 40
REPO = "us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh"
DIGEST = "sha256:" + "b" * 64


class Scripted:
    def __init__(self, *, dirty="", head=HEAD, label=HEAD, build_rc=0, push_rc=0) -> None:
        self.dirty, self.head, self.label, self.build_rc, self.push_rc = dirty, head, label, build_rc, push_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["git", "status", "--porcelain"]:
            return pf.Result(0, self.dirty, "")
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return pf.Result(0, self.head + "\n", "")
        if argv[0] == "make":
            return pf.Result(self.build_rc, "", "build failed")
        if argv[:2] == ["docker", "push"]:
            return pf.Result(self.push_rc, "", "denied")
        if argv[:4] == ["gcloud", "artifacts", "docker", "images"]:
            return pf.Result(0, json.dumps({"image_summary": {"digest": DIGEST}}), "")
        if argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            labels = {} if self.label is None else {pf.REVISION_LABEL: self.label}
            return pf.Result(0, json.dumps({"config": {"Labels": labels}}), "")
        raise AssertionError(f"unexpected call {argv}")


def test_a_clean_tree_builds_pushes_and_returns_the_pin_and_the_verified_commit() -> None:
    run = Scripted()
    built = ib.build_and_push(REPO, runner=run)
    assert built.image == f"{REPO}@{DIGEST}" and built.image_commit == HEAD
    assert built.link == f"verified: {pf.REVISION_LABEL}={HEAD}"
    make = next(c for c in run.calls if c[0] == "make")
    assert make == ["make", "mesh-image", f"MESH_IMAGE={REPO}:{HEAD[:12]}"]
    assert ["docker", "push", f"{REPO}:{HEAD[:12]}"] in run.calls


def test_a_dirty_tree_is_refused_before_anything_is_built() -> None:
    run = Scripted(dirty=" M src/x.py\n")
    with pytest.raises(ib.BuildRefused, match="uncommitted"):
        ib.build_and_push(REPO, runner=run)
    assert not any(c[0] in ("make", "docker") for c in run.calls)


def test_a_pushed_image_whose_label_is_not_the_commit_is_refused() -> None:
    with pytest.raises(ValueError, match="built from"):
        ib.build_and_push(REPO, runner=Scripted(label="dirty"))
    with pytest.raises(ValueError, match="no org.opencontainers.image.revision"):
        ib.build_and_push(REPO, runner=Scripted(label=None))


@pytest.mark.parametrize("what", ["build_rc", "push_rc"])
def test_a_failed_build_or_push_is_refused(what) -> None:
    with pytest.raises(ib.BuildRefused):
        ib.build_and_push(REPO, runner=Scripted(**{what: 1}))


def test_the_makefile_stamps_the_label_only_from_a_clean_tree() -> None:
    makefile = (V4.parents[2] / "Makefile").read_text()
    assert 'SOURCE_REVISION = $(shell test -z "$$(git status --porcelain' in makefile
    assert "|| echo dirty)" in makefile
    mesh = next(line for line in makefile.splitlines() if "docker build --target mesh " in line)
    assert '--label org.opencontainers.image.revision="$(SOURCE_REVISION)"' in mesh


def test_the_build_and_the_push_are_not_held_to_a_registry_reads_two_minutes(monkeypatch) -> None:
    """The first real build was refused: `make mesh-image` ran through preflight's runner, whose 120 s limit is for
    gcloud reads. Building and pushing use the long runner; reads keep the short one."""
    seen: dict[str, float | None] = {}

    def fake_run(argv, capture_output, text, timeout):  # noqa: ANN001, ANN202
        seen[argv[0] + " " + argv[1]] = timeout
        return type("D", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(ib.subprocess, "run", fake_run)
    ib._long_runner(["make", "mesh-image"])
    ib._long_runner(["docker", "push", "x"])
    assert seen["make mesh-image"] >= 3 * 3600 and seen["docker push"] >= 3 * 3600
