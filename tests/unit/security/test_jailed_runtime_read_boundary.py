# Responsibility: Guard the read boundary the jailed interpreter runs inside.
# Owns: the derivation invariants and the behavioural probes that prove enforcement was active.
# Boundaries: it exercises the shipped jail; it defines no policy and patches nothing.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from meshpipeline.sandbox import sandbox_exec as se

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    not se._LANDLOCK_READY,
    reason="Landlock is not enforceable on this kernel - the boundary cannot be observed")


def _run(code: str, workspace: Path, python: str | None = None):
    ws_fd = os.open(str(workspace), os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        return subprocess.run([python or sys.executable, "-c", code], cwd=str(workspace),
                              capture_output=True, text=True, timeout=60,
                              preexec_fn=se.jail_preexec(require_seccomp=True,
                                                         workspace_fd=ws_fd))
    finally:
        os.close(ws_fd)


# the derivation
def test_the_derived_roots_never_include_home_the_repo_or_the_environments_parent():
    roots = set(se._RUNTIME_RO_PATHS)
    for forbidden in (os.path.realpath(os.path.expanduser("~")),
                      os.path.realpath(str(REPO)),
                      os.path.realpath(str(Path(sys.prefix).parent))):
        assert forbidden not in roots, f"the jail grants read access to {forbidden}"


def test_the_derived_roots_are_canonical_and_are_directories():
    for root in se._RUNTIME_RO_PATHS:
        assert root == os.path.realpath(root), f"{root} is not canonical - a symlink could widen it"
        assert Path(root).is_dir(), f"{root} is not a directory"


def test_a_root_already_covered_by_the_system_set_is_not_re_added():
    for root in se._RUNTIME_RO_PATHS:
        assert not any(se._is_within(root, sysroot) for sysroot in se._RO_PATHS), (
            f"{root} is already covered by _RO_PATHS and should have been dropped")


def test_containment_is_compared_by_path_component_not_string_prefix():
    assert se._is_within("/usr/lib/python3.12", "/usr")
    assert se._is_within("/usr", "/usr")
    assert not se._is_within("/usrlocal", "/usr")
    assert not se._is_within("/usr", "/usr/lib")


# the behaviour
def test_the_jailed_interpreter_starts_and_runs(tmp_path):
    r = _run("print(round(2.0 ** 0.5, 4))", tmp_path)
    assert r.returncode == 0, r.stderr[:400]
    assert "1.4142" in r.stdout


def test_enforcement_is_actually_active_while_those_tests_run(tmp_path):
    secret = tmp_path.parent / "enforcement_probe.txt"
    secret.write_text("must not be readable")
    try:
        r = _run(f"open({str(secret)!r}).read(); print('READ')", tmp_path)
        assert r.returncode != 0 and "READ" not in r.stdout, (
            "a read outside the workspace succeeded - Landlock was not enforcing")
    finally:
        secret.unlink()


def test_the_runtime_roots_are_readable_but_not_writable(tmp_path):
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    if cfg.exists():
        r = _run(f"print(len(open({str(cfg)!r}).read()) > 0)", tmp_path)
        assert r.returncode == 0 and "True" in r.stdout, "the interpreter cannot read pyvenv.cfg"

    probe = Path(sys.prefix) / "landlock_write_probe.txt"
    r = _run(f"open({str(probe)!r},'w').write('x')", tmp_path)
    wrote = probe.exists()
    if wrote:
        probe.unlink()
    assert r.returncode != 0 and not wrote, "the runtime root is writable - it must be read-only"


def test_a_sibling_of_the_environment_stays_unreadable(tmp_path):
    sibling = Path(sys.prefix).parent / "landlock_sibling_probe.txt"
    if sibling.exists():
        pytest.skip("probe path already exists; refusing to touch it")
    sibling.write_text("must stay unreadable")
    try:
        r = _run(f"open({str(sibling)!r}).read(); print('READ')", tmp_path)
        assert r.returncode != 0 and "READ" not in r.stdout, (
            "granting the runtime root exposed its parent directory")
    finally:
        sibling.unlink()


def test_a_symlink_inside_a_runtime_root_cannot_widen_the_boundary(tmp_path):
    target = tmp_path.parent / "symlink_target_probe.txt"
    target.write_text("must not be reachable through a link")
    link = Path(sys.prefix) / "landlock_escape_link"
    if link.exists():
        pytest.skip("probe path already exists; refusing to touch it")
    link.symlink_to(target)
    try:
        r = _run(f"open({str(link)!r}).read(); print('READ')", tmp_path)
        assert r.returncode != 0 and "READ" not in r.stdout, (
            "a symlink inside a granted root reached outside it")
    finally:
        link.unlink()
        target.unlink()


def test_a_write_inside_the_workspace_still_succeeds(tmp_path):
    r = _run("open('inside.txt','w').write('ok')", tmp_path)
    assert r.returncode == 0, r.stderr[:300]
    assert (tmp_path / "inside.txt").read_text() == "ok"


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system interpreter")
def test_a_system_interpreter_still_runs(tmp_path):
    r = _run("print('SYSPY')", tmp_path, python="/usr/bin/python3")
    assert r.returncode == 0 and "SYSPY" in r.stdout, r.stderr[:300]


def test_no_granted_root_is_a_directory_applications_install_into():
    application_roots = {"/opt", "/srv", "/home", "/var", "/mnt", "/media", "/data"}
    offenders = [p for p in se._RO_PATHS if p.rstrip("/") in application_roots]
    assert offenders == [], (
        f"{offenders} are directories applications install into; grant the specific subdirectory "
        "the runtime needs instead of the whole root")


def test_the_runtime_root_is_granted_even_when_the_app_lives_under_opt(tmp_path, monkeypatch):
    monkeypatch.setattr(se.sys, "prefix", "/opt/someapp/.venv", raising=False)
    monkeypatch.setattr(se.sys, "base_prefix", "/usr", raising=False)
    monkeypatch.setattr(se.os.path, "isdir", lambda p: True)
    roots = se._runtime_ro_roots()
    assert any(r.startswith("/opt/someapp") for r in roots), (
        f"an interpreter under /opt/someapp produced no runtime root: {roots}")
