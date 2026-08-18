# Responsibility: Verify the release-tag policy refuses every unpromotable state and creates nothing.
# Boundaries: disposable git repositories only; the real repository is never tagged, and no remote is contacted.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.release import tag_policy as tp

VERSION = "0.1"


def _repo(tmp_path: Path, version: str = VERSION) -> Path:
    # A throwaway repository with its own object store. Tag behaviour is proven HERE and never in
    # the working repository, which must finish this suite with zero tags.
    root = tmp_path / "repo"
    (root / "src" / "meshpipeline").mkdir(parents=True)
    (root / "src" / "meshpipeline" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    def run(*a):
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "policy@test.invalid")
    run("config", "user.name", "policy test")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    return root


def _tag(repo: Path, name: str, *, annotated: bool = True) -> None:
    args = ["tag", "-a", name, "-m", name] if annotated else ["tag", name]
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _record(verdict: str = "passed") -> dict:
    # A record the REAL promotability authority accepts: every identity field, a digest-qualified
    # component, and a verdict that agrees with its own checks. Built here rather than stubbed, so
    # a change to what "promotable" means reaches this suite instead of passing it by.
    from devtools.release import record as rec
    checks = [{"check": "wheel build", "status": verdict, "required": True}]
    digest = "sha256:" + "a" * 64
    return {
        "record_schema": rec.SCHEMA_VERSION,
        "commit": "b" * 40,
        "tree": "c" * 40,
        "product_version": VERSION,
        "schema_versions": {"final_result": 1, "pipeline_state": 1},
        "wheel": {"filename": f"meshpipeline-{VERSION}-py3-none-any.whl",
                  "sha256": "d" * 64, "bytes": 12345},
        "verdict": rec.compute_verdict(checks),
        "checks": checks,
        "state": "published",
        "components": {"api": {"local_image_id": "sha256:" + "e" * 64,
                               "registry_digest": digest,
                               "reference": f"registry.example/meshpipeline-api@{digest}"}},
    }


# tree and tag state


def test_a_dirty_tree_refuses_promotion(tmp_path):
    repo = _repo(tmp_path)
    tp.check_clean_tree(repo)
    (repo / "src" / "meshpipeline" / "stray.py").write_text("x = 1\n")
    with pytest.raises(tp.Refused) as r:
        tp.check_clean_tree(repo)
    assert "stray.py" in str(r.value)


def test_a_lightweight_tag_refuses(tmp_path):
    repo = _repo(tmp_path)
    _tag(repo, "v0.1", annotated=False)
    with pytest.raises(tp.Refused) as r:
        tp.check_tag_is_annotated("v0.1", repo)
    assert "lightweight" in str(r.value)


def test_an_annotated_tag_is_accepted(tmp_path):
    repo = _repo(tmp_path)
    _tag(repo, "v0.1", annotated=True)
    tp.check_tag_is_annotated("v0.1", repo)


def test_an_existing_tag_cannot_be_replaced(tmp_path):
    repo = _repo(tmp_path)
    tp.check_tag_is_new("v0.1", repo)
    _tag(repo, "v0.1")
    with pytest.raises(tp.Refused) as r:
        tp.check_tag_is_new("v0.1", repo)
    assert "immutable" in str(r.value)


# gate evidence


def test_promotion_refuses_while_the_gates_are_unproven():
    with pytest.raises(tp.Refused) as r:
        tp.check_gates_proven(None)
    assert "unproven" in str(r.value)
    with pytest.raises(tp.Refused):
        tp.check_gates_proven(_record(verdict="failed"))
    tp.check_gates_proven(_record())


# the readiness path itself


def test_readiness_creates_no_tag_and_touches_no_remote(tmp_path):
    repo = _repo(tmp_path)
    before = subprocess.run(["git", "for-each-ref", "refs/tags"], cwd=repo,
                            capture_output=True, text=True).stdout
    refusals = tp.readiness("v0.1", record=_record(), repo=repo)
    after = subprocess.run(["git", "for-each-ref", "refs/tags"], cwd=repo,
                           capture_output=True, text=True).stdout
    assert refusals == [], refusals
    assert before == after == "", "readiness validation created a ref"


def test_readiness_reports_every_reason_at_once(tmp_path):
    repo = _repo(tmp_path, version="0.0.1")
    (repo / "dirty.txt").write_text("x")
    refusals = tp.readiness("v0.1", record=None, repo=repo)
    joined = "\n".join(refusals)
    assert len(refusals) == 2, refusals
    for expected in ("clean tree", "unproven"):
        assert expected in joined, f"{expected!r} missing from:\n{joined}"


def test_readiness_can_run_without_network_access(tmp_path, monkeypatch):
    # Absence of a remote must never stop local readiness validation. The disposable repository has
    # no remote at all, which is the strongest form of that.
    repo = _repo(tmp_path)
    assert subprocess.run(["git", "remote"], cwd=repo, capture_output=True,
                          text=True).stdout.strip() == ""
    assert tp.readiness("v0.1", record=_record(), repo=repo) == []


@pytest.mark.parametrize("forbidden", ["push", "fetch", "pull", "ls-remote", "--delete", "--force"])
def test_the_policy_cannot_run_a_writing_or_remote_git_command(forbidden, tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AssertionError) as r:
        tp._git("tag", forbidden, repo=repo)
    assert "publish" in str(r.value) or "write" in str(r.value)


def test_the_policy_cannot_run_an_unlisted_git_subcommand(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AssertionError):
        tp._git("commit", "-m", "no", repo=repo)
