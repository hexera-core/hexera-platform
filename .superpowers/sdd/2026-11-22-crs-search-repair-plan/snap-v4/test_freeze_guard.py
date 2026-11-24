# Responsibility: lane C14's freeze guard: the held-out run REFUSES unless a freeze tag names the run commit and that
#                 commit contains every required commit. Both ways, on a throwaway git repository.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freeze_guard  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    for i in range(3):
        (tmp_path / "f").write_text(str(i))
        _git(tmp_path, "add", "f")
        _git(tmp_path, "commit", "-q", "-m", f"c{i}")
    return tmp_path


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref)


def test_no_freeze_tag_refuses(repo: Path) -> None:
    ok, msg = freeze_guard.check(str(repo), "HEAD")
    assert not ok and "no git tag matches 'crs-v4-freeze-*'" in msg


def test_a_tag_that_does_not_match_the_pattern_refuses(repo: Path) -> None:
    _git(repo, "tag", "v1.0", "HEAD")
    ok, msg = freeze_guard.check(str(repo), "HEAD")
    assert not ok and "no git tag matches" in msg


def test_a_freeze_tag_on_another_commit_refuses(repo: Path) -> None:
    _git(repo, "tag", "crs-v4-freeze-20261124", "HEAD~1")
    ok, msg = freeze_guard.check(str(repo), "HEAD")
    assert not ok and "is not the commit of any freeze tag" in msg and "crs-v4-freeze-20261124" in msg


@pytest.mark.parametrize("annotated", [False, True])
def test_a_matching_freeze_tag_proceeds(repo: Path, annotated: bool) -> None:
    args = ["-a", "-m", "freeze"] if annotated else []
    _git(repo, "tag", *args, "crs-v4-freeze-20261124", "HEAD")
    ok, msg = freeze_guard.check(str(repo), "crs-v4-freeze-20261124")  # by tag name
    assert ok and msg == f"FROZEN crs-v4-freeze-20261124 {_sha(repo)}"
    assert freeze_guard.check(str(repo), _sha(repo))[0]                 # and by the sha it names


def test_a_required_commit_missing_from_the_frozen_one_refuses(repo: Path) -> None:
    _git(repo, "tag", "crs-v4-freeze-x", "HEAD~1")
    ok, msg = freeze_guard.check(str(repo), "HEAD~1", require=(_sha(repo),))  # HEAD is not in HEAD~1's history
    assert not ok and "does not contain required commit" in msg
    assert freeze_guard.check(str(repo), "HEAD~1", require=(_sha(repo, "HEAD~2"),))[0]


def test_the_cli_exits_1_with_the_reason_and_0_with_the_tag(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert freeze_guard.main(["--repo", str(repo), "--commit", "HEAD"]) == 1
    assert "REFUSED" in capsys.readouterr().err
    _git(repo, "tag", "crs-v4-freeze-1", "HEAD")
    assert freeze_guard.main(["--repo", str(repo), "--commit", "HEAD"]) == 0
    assert capsys.readouterr().out.startswith("FROZEN crs-v4-freeze-1")
