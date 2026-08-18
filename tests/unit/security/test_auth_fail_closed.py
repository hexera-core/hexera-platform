# Responsibility: Verify production refuses to start without its secrets, while a bare clone still runs in dev.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

_IMPORT = "import meshpipeline.runtime.startup"


def _boots(**env) -> tuple[bool, str]:
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}
    base.update(env)
    p = subprocess.run([sys.executable, "-c", _IMPORT], env=base,
                       capture_output=True, text=True)
    return p.returncode == 0, p.stderr


def test_prod_without_secrets_refuses_to_start():
    ok, err = _boots(ENV="production")
    assert not ok, "prod booted with no auth secrets - silent bypass"
    assert "MESH_API_KEY" in err and "USER_TOKEN_SECRET" in err
    assert "self-asserted" in err


def test_staging_with_only_one_secret_refuses_to_start():
    ok, err = _boots(ENV="staging", MESH_API_KEY="k")
    assert not ok, "staging booted with USER_TOKEN_SECRET missing - still wide open"
    assert "USER_TOKEN_SECRET" in err


def test_prod_with_both_secrets_boots():
    ok, err = _boots(ENV="production", MESH_API_KEY="k", USER_TOKEN_SECRET="s")
    assert ok, f"prod refused to start WITH both secrets set: {err[-400:]}"


def test_dev_without_secrets_still_boots():
    # genuinely-local single-tenant use is explicitly allowed to run self-asserted
    ok, err = _boots(ENV="dev")
    assert ok, f"dev refused to start: {err[-400:]}"


def test_the_default_env_is_dev_so_a_bare_clone_runs_locally():
    ok, _ = _boots()   # no ENV set at all → defaults to dev
    assert ok
