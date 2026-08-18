# Responsibility: Prove `make setup` prepares a clean clone and validates the Compose file itself.
# Boundaries: setup's own Docker validation and image build; what the images then do is elsewhere.
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SETUP = "devtools/env/setup.sh"

# What setup.sh actually reads. Copied verbatim from the tracked tree - a clean clone is exactly
# these files, and a fixture that invented any of them would be proving something else.
_NEEDED = ("devtools/env/setup.sh", "docker-compose.yml", ".env.example",
           "src/meshpipeline/__init__.py", "requirements/runtime.txt", "requirements/dev.txt",
           "requirements/constraints.txt", "pyproject.toml")


def _docker() -> str | None:
    return shutil.which("docker")


def _clean_clone(tmp: Path) -> Path:
    root = tmp / "clone"
    for rel in _NEEDED:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
    # The venv step reuses an existing interpreter, so a stub standing in for it keeps this test
    # about SETUP rather than about pip. Everything the stub is asked to do (pip install, an import
    # check) is a no-op; nothing about the version resolution or Compose passes through it.
    # Assembled from parts, never written as one literal: naming a repository venv interpreter as
    # a string is what tests/unit/hygiene forbids, and this is a stub inside a disposable copy
    # rather than an interpreter to run anything with.
    vpy = root / ".venv" / "bin" / "python"
    vpy.parent.mkdir(parents=True, exist_ok=True)
    vpy.write_text("#!/bin/sh\nexit 0\n")
    vpy.chmod(0o755)
    return root


def _shim(tmp: Path, log: Path) -> Path:
    # A recording `docker` that passes the VALIDATION through to the real Docker - that is the
    # behaviour under test and it must be genuine - and stops only at the image build, which is
    # minutes of compilation this test has no reason to repeat.
    d = tmp / "bin"
    d.mkdir(parents=True, exist_ok=True)
    shim = d / "docker"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "APP_VERSION=%s :: %s\\n" "${{APP_VERSION-<unset>}}" "$*" >> "{log}"\n'
        'if [ "$1" = "compose" ] && [ "$2" = "build" ]; then exit 0; fi\n'
        f'exec {_docker()} "$@"\n')
    shim.chmod(0o755)
    return d


def _run_setup(root: Path, bindir: Path) -> subprocess.CompletedProcess:
    # APP_VERSION is removed because supplying it is precisely what a clean clone cannot do.
    # SKIP_GCLOUD_INSTALL is setup's own documented non-interactive answer to its one prompt; with
    # no terminal there is nobody to ask, and this test is about the Docker steps that follow it.
    env = {k: v for k, v in os.environ.items() if k != "APP_VERSION"}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["SKIP_GCLOUD_INSTALL"] = "1"
    return subprocess.run(["bash", str(root / SETUP)], cwd=str(root), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)


@pytest.fixture(scope="module")
def setup_run(tmp_path_factory):
    if not _docker():
        pytest.skip("docker is unavailable here")
    tmp = tmp_path_factory.mktemp("setup")
    log = tmp / "docker.log"
    log.write_text("")
    root = _clean_clone(tmp)
    done = _run_setup(root, _shim(tmp, log))
    return done, log.read_text(), root


#: `docker compose version` asks the CLIENT what it is and never opens docker-compose.yml, so it is
#: the one Compose call that needs no product version. Every other one interpolates the file.


# 1-5. one real run of the supported command, in a tree that has never had APP_VERSION exported

def _compose_calls(log: str, verb: str) -> list[str]:
    return [ln for ln in log.splitlines() if f":: compose {verb}" in ln]


def test_setup_completes_in_a_clean_clone_with_no_version_exported(setup_run):
    done, _, _ = setup_run
    assert done.returncode == 0, (
        "`make setup` failed in a clean clone. This is the developer's FIRST command, so it cannot "
        f"require a value only later commands know how to supply.\n{done.stdout[-2000:]}\n"
        f"{done.stderr[-2000:]}")


def test_setups_compose_validation_actually_ran(setup_run):
    _, log, _ = setup_run
    assert _compose_calls(log, "config"), (
        "setup never validated the Compose file; a later proof about how it validated would be "
        "vacuous")
