# Responsibility: Verify the sandboxed interpreter is jailed, scanned, and confined at the filesystem level.
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.sandbox.safe_exec import scan_python_source  # noqa: E402  (pure stdlib)


def test_scan_passes_legit_numeric_code():
    ok = "import numpy as np\nx = np.linalg.norm([3,4])\nprint(x)\n"
    assert scan_python_source(ok) is None


def test_scan_blocks_network_and_escape_vectors():
    cases = {
        "import socket":                 "socket",
        "import subprocess":             "subprocess",
        "import importlib":              "importlib",
        "import ctypes":                 "ctypes",
        "__import__('socket')":          "__import__",
        "eval('1+1')":                   "eval",
        "exec('x=1')":                   "exec",
        "import os\nos.system('id')":    "system",
        "import os\ngetattr(os,'system')('id')": "getattr",
        "import os\nprint(os.environ)":  "environ",
    }
    for src, needle in cases.items():
        reason = scan_python_source(src)
        assert reason is not None, f"scan should reject: {src!r}"
        assert needle in reason, f"{needle!r} not flagged for {src!r}: {reason}"


def test_sandbox_module_wired_and_preexec_callable():
    from meshpipeline.sandbox import sandbox_exec
    assert isinstance(sandbox_exec.seccomp_supported(), bool)
    pre = sandbox_exec.jail_preexec(require_seccomp=True)
    assert callable(pre)


def test_run_python_uses_jail_and_scan():
    # run_python lives in the research family since.
    src = (APP / "agents" / "builder" / "tools" / "research.py").read_text()
    assert "from meshpipeline.sandbox.sandbox_exec import jail_preexec" in src
    assert "preexec_fn=jail_preexec" in src
    assert "scan_python_source" in src
    # filesystem confinement: the workspace dir fd is opened and passed to the jail
    assert "O_PATH" in src and "workspace_fd=_ws_fd" in src


def test_sandbox_has_landlock_fs_confinement():
    from meshpipeline.sandbox import sandbox_exec
    assert isinstance(sandbox_exec.landlock_supported(), bool)
    assert hasattr(sandbox_exec, "_apply_landlock")
    # jail_preexec accepts a workspace_fd for Landlock confinement
    import inspect
    assert "workspace_fd" in inspect.signature(sandbox_exec.jail_preexec).parameters


# USER_TOKEN_SECRET enforcement is not asserted here any more. It was a source-string check that
# startup.py contained the literal `if ENV == "production"` block - a branch settings.policy made
# unreachable, since policy refuses the import of any hardened environment missing that secret.
# The real contract is proven by BOOTING the process:
#   tests/unit/security/test_auth_fail_closed.py       - refusal happens, and happens at import
#   tests/unit/settings/test_hardened_runtime_policy.py - for every hosted environment name


def test_global_exception_handler_present():
    src = (APP / "api" / "app.py").read_text()
    assert "@app.exception_handler(Exception)" in src
    assert "Internal server error" in src
