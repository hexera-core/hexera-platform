# Responsibility: Verify the image-freshness digest covers every tracked path and nothing untracked.
# Boundaries: the digest itself - whether a rebuild then stamps it is the rebuild-authority suite.
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tests" / "integration" / "source_digest.sh"

#: A miniature repository with one file in every kind of place the real one has. Every case below
#: runs against a throwaway clone of this, so no test can leave the real tree modified.
SEED = {
    "Dockerfile": "FROM scratch\nCOPY src/ ./src/\nCOPY ui/ ./ui/\nCOPY alembic.ini .\n",
    "docker-compose.yml": "services:\n  api:\n    build: .\n",
    "Makefile": "all:\n\techo hi\n",
    "pyproject.toml": "[project]\nname = 'x'\n",
    "alembic.ini": "[alembic]\n",
    "src/app.py": "print('one')\n",
    "ui/index.html": "<h1>one</h1>\n",
    "deploy/docker/entrypoint.sh": "#!/bin/sh\necho one\n",
    "alembic/versions/0001.py": "revision = '0001'\n",
    "requirements/runtime.txt": "flask==1.0\n",
    "tests/test_x.py": "def test_x(): pass\n",
    "docs/readme.md": "# one\n",
    ".gitignore": "build/\n*.log\n",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout


def _digest(repo: Path) -> str:
    r = subprocess.run(["bash", str(repo / "tests" / "integration" / "source_digest.sh")],
                       cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, f"source_digest failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout.strip()


def _digest_result(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(repo / "tests" / "integration" / "source_digest.sh")],
                          cwd=repo, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    for rel, body in SEED.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (r / "tests" / "integration").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT, r / "tests" / "integration" / "source_digest.sh")
    _git(r.parent, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


# 1. determinism


def test_byte_identical_trees_produce_the_same_digest(repo):
    assert _digest(repo) == _digest(repo)


def test_a_second_identical_checkout_produces_the_same_digest(repo, tmp_path):
    twin = tmp_path / "twin"
    shutil.copytree(repo, twin)
    assert _digest(repo) == _digest(twin), "the digest depends on something outside the contents"


# 2-7. every tracked area counts


@pytest.mark.parametrize("rel,new", [
    ("src/app.py", "print('two')\n"),
    ("Dockerfile", "FROM scratch\nCOPY src/ ./src/\nCOPY ui/ ./ui/\nCOPY alembic.ini .\nENV A=1\n"),
    ("ui/index.html", "<h1>two</h1>\n"),
    ("deploy/docker/entrypoint.sh", "#!/bin/sh\necho two\n"),
    ("docker-compose.yml", "services:\n  api:\n    build: .\n    ports: ['1:1']\n"),
    ("tests/test_x.py", "def test_x(): assert True\n"),
    ("docs/readme.md", "# two\n"),
    ("requirements/runtime.txt", "flask==2.0\n"),
    ("Makefile", "all:\n\techo bye\n"),
    ("alembic/versions/0001.py", "revision = '0002'\n"),
])
def test_an_edit_anywhere_in_the_tracked_tree_changes_the_digest(repo, rel, new):
    before = _digest(repo)
    (repo / rel).write_text(new)
    assert _digest(repo) != before, f"a change to {rel} left the digest unchanged"


# 8-9. working tree, not history or index


def test_an_unstaged_edit_changes_the_digest(repo):
    before = _digest(repo)
    (repo / "src" / "app.py").write_text("print('unstaged')\n")
    assert _git(repo, "status", "--porcelain").strip(), "the edit did not register as unstaged"
    assert _digest(repo) != before


def test_staged_then_modified_content_reflects_the_working_file(repo):
    (repo / "src" / "app.py").write_text("print('staged')\n")
    _git(repo, "add", "src/app.py")
    staged = _digest(repo)
    (repo / "src" / "app.py").write_text("print('and then modified again')\n")
    assert _digest(repo) != staged, \
        "the digest followed the index; the file on disk is what would be built"


def test_a_tracked_addition_changes_the_digest(repo):
    before = _digest(repo)
    (repo / "src" / "extra.py").write_text("x = 1\n")
    _git(repo, "add", "src/extra.py")
    assert _digest(repo) != before


# 10-13. deletions, renames, modes, symlinks


def test_a_tracked_deletion_changes_the_digest(repo):
    before = _digest(repo)
    (repo / "src" / "app.py").unlink()
    assert _digest(repo) != before, "a deleted tracked file left the digest unchanged"


def test_a_staged_deletion_and_an_unstaged_one_are_distinguishable(repo):
    # Both remove the file from disk, but only the staged one removes it from the index. Without a
    # distinct record for "tracked, still in the index, gone from disk" the two collapse into the
    # same digest, and a half-finished deletion looks like a finished one.
    (repo / "src" / "app.py").unlink()
    unstaged = _digest(repo)
    _git(repo, "rm", "-q", "--cached", "src/app.py")
    staged = _digest(repo)
    assert unstaged != staged, \
        "a deleted-but-still-indexed file and a fully removed one produce the same digest"


def test_a_tracked_rename_changes_the_digest(repo):
    before = _digest(repo)
    _git(repo, "mv", "src/app.py", "src/renamed.py")
    assert _digest(repo) != before, "the path is not part of the digest"


def test_an_executable_bit_change_changes_the_digest(repo):
    target = repo / "deploy" / "docker" / "entrypoint.sh"
    before = _digest(repo)
    os.chmod(target, 0o755)
    assert _digest(repo) != before, "file mode is not part of the digest"


def test_a_symlink_target_change_changes_the_digest_without_dereferencing(repo):
    link = repo / "src" / "link"
    link.symlink_to("app.py")
    _git(repo, "add", "src/link")
    before = _digest(repo)
    link.unlink()
    link.symlink_to("elsewhere.py")            # a target that does not exist
    assert _digest(repo) != before, "the symlink target is not part of the digest"
    assert not link.resolve().exists(), "the fixture no longer proves the no-dereference case"


def test_a_symlink_and_a_regular_file_with_the_same_bytes_differ(repo):
    (repo / "src" / "plain").write_text("app.py")
    link = repo / "src" / "linky"
    link.symlink_to("app.py")
    _git(repo, "add", "-A")
    # Same content bytes, different mode: the records must not collapse into one another.
    assert _digest(repo) == _digest(repo)
    before = _digest(repo)
    link.unlink()
    (repo / "src" / "linky").write_text("app.py")
    _git(repo, "add", "-A")
    assert _digest(repo) != before, "a symlink and a regular file of the same bytes look identical"


# 14-15. untracked and ignored content stays out


def test_untracked_evidence_outside_the_copy_roots_does_not_change_the_digest(repo):
    before = _digest(repo)
    (repo / "BETA").mkdir()
    (repo / "BETA" / "round.txt").write_text("evidence\n")
    (repo / "output").mkdir()
    (repo / "output" / "mesh.stl").write_text("solid\n")
    assert _digest(repo) == before, "untracked evidence leaked into the digest"


def test_ignored_build_residue_does_not_change_the_digest(repo):
    before = _digest(repo)
    (repo / "build").mkdir()
    (repo / "build" / "artifact.o").write_text("binary\n")
    (repo / "noise.log").write_text("log\n")
    assert _digest(repo) == before, "ignored residue leaked into the digest"


# 16. unusual filenames


@pytest.mark.parametrize("name", [
    "with space.py", "with\nnewline.py", "with'quote.py", 'with"dquote.py',
    "with$dollar.py", "with;semi.py", "with\ttab.py", "-leading-dash.py", "with*star.py",
])
def test_unusual_filenames_are_framed_without_breaking_enumeration(repo, name):
    before = _digest(repo)
    (repo / "src" / name).write_text("x = 1\n")
    _git(repo, "add", "-A")
    after = _digest(repo)
    assert after and after != before, f"{name!r} was not enumerated"
    assert len(after) == 64, "the digest stopped being a single sha256"


def test_two_paths_that_differ_only_by_a_delimiter_do_not_collide(repo):
    # A newline-delimited or space-separated framing would let these two produce one record.
    (repo / "src" / "a b").write_text("same\n")
    _git(repo, "add", "-A")
    one = _digest(repo)
    (repo / "src" / "a b").unlink()
    (repo / "src" / "a").mkdir(exist_ok=True)
    (repo / "src" / "a" / "b").write_text("same\n")
    _git(repo, "add", "-A")
    assert _digest(repo) != one, "'src/a b' and 'src/a/b' collided"


# the copy boundary


def test_an_untracked_file_inside_a_copy_root_is_refused(repo):
    (repo / "src" / "sneaky.py").write_text("would ship, would not be hashed\n")
    r = _digest_result(repo)
    assert r.returncode != 0, (
        "an untracked file inside a Dockerfile COPY source was accepted; it would ship in the "
        "image while being absent from the digest")
    assert "sneaky.py" in r.stderr, "the refusal does not name the offending file"


def test_an_untracked_file_outside_every_copy_root_is_fine(repo):
    (repo / "docs" / "scratch.md").write_text("not copied into the image\n")
    assert _digest_result(repo).returncode == 0, \
        "a path the Dockerfile never copies was treated as a build-context leak"


def test_the_copy_roots_come_from_the_dockerfile_not_a_hardcoded_list(repo):
    # Point the Dockerfile at a different directory; the boundary must move with it.
    (repo / "Dockerfile").write_text("FROM scratch\nCOPY docs/ ./docs/\n")
    _git(repo, "add", "-A")
    (repo / "docs" / "stray.md").write_text("now inside a copy root\n")
    assert _digest_result(repo).returncode != 0, \
        "the copy roots are hardcoded; they must be read from the Dockerfile"


# structural guards - each names a way the digest was, or could be, silently narrowed

SCRIPT_TEXT = SCRIPT.read_text()
CODE = "\n".join(ln for ln in SCRIPT_TEXT.splitlines() if not ln.lstrip().startswith("#"))


def test_the_enumeration_is_not_a_curated_directory_allowlist():
    # `git ls-files -- src alembic ...` is exactly the shape that let a Dockerfile edit through.
    assert "ls-files" in CODE, "the digest no longer enumerates tracked files"
    for line in CODE.splitlines():
        if "ls-files" in line:
            assert " -- " not in line, (
                f"the enumeration is limited to a path list again: {line.strip()!r}. Every tracked "
                "path must count; a curated list is what omitted Dockerfiles, ui/ and deploy/docker/.")


def test_the_digest_is_not_derived_from_history_or_the_index():
    for forbidden in ("rev-parse HEAD", "HEAD^{tree}", "write-tree", "cat-file", "diff-index"):
        assert forbidden not in CODE, (
            f"{forbidden!r} reads recorded state, not the working file. An unstaged edit would "
            "become invisible - the exact case that let a stale image be tested against.")
    assert "ls-files -s" in CODE or '"ls-files", "-s"' in CODE, \
        "the index read that identifies submodules is gone"
    # the index may identify gitlinks, but content must come from the working file
    assert "open(full" in CODE and "readlink" in CODE, \
        "the digest no longer reads working-tree contents"


def test_each_record_carries_a_path_and_a_mode_as_well_as_content():
    assert "path_hash" in CODE, "paths are not part of the records; a rename would go unnoticed"
    # The path must be HASHED into the record, never interpolated raw. Records are newline-joined,
    # so a raw path containing a newline could forge a record boundary.
    assert "path_hash = h(path)" in CODE, (
        "the path is put into the record without hashing it. Records are joined with newlines, so "
        "a filename containing one could split or forge a record.")
    appends = [ln for ln in CODE.splitlines() if "records.append" in ln]
    assert appends, "no records are built"
    for ln in appends:
        assert "path_hash" in ln and "fsdecode" not in ln, \
            f"a record interpolates something other than the path hash: {ln.strip()!r}"
    assert "100755" in CODE and "100644" in CODE, \
        "file modes are not part of the records; an executable-bit change would go unnoticed"
    assert "120000" in CODE, "symlinks have no distinct mode"
    assert "000000" in CODE, "a tracked deletion has no record"


def test_enumeration_and_framing_are_nul_safe():
    assert 'split(b"\\0")' in CODE, "output is no longer split on NUL"
    for line in CODE.splitlines():
        if "ls-files" in line or "porcelain" in line:
            assert '"-z"' in line or "porcelain=v1" in line, \
                f"an enumeration is not NUL-delimited: {line.strip()!r}"


def test_the_copy_boundary_is_enforced_and_not_bypassed():
    assert "--others" in CODE and "--exclude-standard" in CODE, \
        "the untracked-in-a-copy-root check is gone; unhashed content could ship in the image"
    assert "sys.exit" in CODE, "the copy-boundary check no longer refuses"


def test_no_other_component_reimplements_the_digest():
    makefile = (REPO / "Makefile").read_text()
    runner = (REPO / "tests" / "integration" / "run_disposable.sh").read_text()
    compose = (REPO / "docker-compose.yml").read_text()
    preflight = (REPO / "tests" / "integration" / "preflight.py").read_text()
    for name, text in (("Makefile", makefile), ("run_disposable.sh", runner),
                       ("preflight.py", preflight), ("docker-compose.yml", compose)):
        # run_disposable.sh reaches the digest THROUGH preflight.py, which is still one authority
        # and not a second implementation.
        assert ("source_digest.sh" in text or "SOURCE_DIGEST" in text
                or name == "docker-compose.yml"
                or (name == "run_disposable.sh" and "preflight.py" in text))
        for algo in ("sha256sum", "hashlib", "git ls-files"):
            if algo in text:
                assert "source_digest.sh" in text, \
                    f"{name} computes a digest of its own instead of calling the one authority"
