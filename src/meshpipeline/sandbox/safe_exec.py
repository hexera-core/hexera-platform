# Responsibility: Run a native subprocess under bounds, and describe truthfully how it ended.
# Owns: the outcome type, signal naming, the scrubbed environment and the source scan.
# Boundaries: a timeout, a signal and a non-zero exit are distinct outcomes and stay distinct.
from __future__ import annotations

import ast
import logging
import os
import re
import signal
import subprocess
from enum import Enum

logger = logging.getLogger(__name__)

#: How much of a native tool's final output an operator record keeps. Bounded so a runaway log
#: cannot become the failure record, and large enough to hold the last real message.
_OUTPUT_TAIL_BYTES = 2000


class NativeOutcome(str, Enum):

    ok = "ok"
    failed = "failed"                 # ordinary non-zero exit: the tool reported a problem
    signalled = "signalled"           # killed by a signal: SIGSEGV, SIGKILL, SIGTERM, ...
    timed_out = "timed_out"           # we stopped it at the deadline
    cancelled = "cancelled"           # we stopped it because the work was abandoned


def signal_name(returncode: int) -> str:
    try:
        return signal.Signals(-returncode).name
    except (ValueError, TypeError):
        return f"signal {-returncode}"


def command_identity(args) -> str:
    if isinstance(args, (str, bytes)):
        args = [args]
    parts = [str(a) for a in (args or [])]
    if not parts:
        return "unknown"
    exe = os.path.basename(parts[0])
    if exe in ("bash", "sh", "zsh"):
        for token in parts[1:]:
            if token.startswith("-"):
                continue
            # A sourced shell line: `source <profile> && <tool> ...`. The profile is an
            # environment file, not the tool, and its base name is exactly the kind of
            # deployment detail this function exists to drop - so the word after `source`
            # is skipped along with redirections.
            words = str(token).replace("&&", " ").replace(";", " ").split()
            skip_next = False
            for word in words:
                if skip_next:
                    skip_next = False
                    continue
                if word in ("source", "."):
                    skip_next = True
                    continue
                if word.startswith(("-", ">", "<", "2>", "|")):
                    continue
                base = os.path.basename(word)
                if base and base not in ("sh", "bash", "zsh", "env"):
                    return f"{exe}:{base}"
            break
    return exe


def describe_native_result(*, returncode: int, args=None, stage: str = "",
                           output: str = "", outcome: NativeOutcome | None = None) -> dict:
    tail = (output or "")[-_OUTPUT_TAIL_BYTES:]
    if outcome is None:
        if returncode == 0:
            outcome = NativeOutcome.ok
        elif returncode < 0:
            outcome = NativeOutcome.signalled
        else:
            outcome = NativeOutcome.failed
    result = {"rc": returncode,
              "timed_out": outcome is NativeOutcome.timed_out,
              "cancelled": outcome is NativeOutcome.cancelled,
              "signalled": outcome is NativeOutcome.signalled,
              "outcome": outcome.value,
              "signal": signal_name(returncode) if outcome is NativeOutcome.signalled else "",
              "command": command_identity(args),
              "stage": stage or ""}
    if outcome is NativeOutcome.signalled:
        last = next((ln for ln in reversed(tail.splitlines()) if ln.strip()), "")
        header = (f"[NATIVE SIGNAL] {result['command']} was killed by {result['signal']} "
                  f"(rc={returncode})"
                  + (f" during {stage}" if stage else "")
                  + (f"; last output before the signal: {last!r}" if last else ""))
        tail = header + "\n" + tail
    result["log_tail"] = tail
    return result


def run_guarded(args, *, timeout=None, **kwargs) -> subprocess.CompletedProcess:
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    kwargs.setdefault("start_new_session", True)   # setsid: child leads its own group
    proc = subprocess.Popen(args, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()         # reap the killed tree
        # Re-raise with the partial output attached, exactly as subprocess.run does,
        # so callers reading exc.stdout / exc.stderr keep working.
        raise subprocess.TimeoutExpired(proc.args, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass

# Drop any env var whose NAME looks like a secret, so model code cannot read keys
# out of os.environ. Legit runtime vars (PATH, LD_LIBRARY_PATH, OMP_*, VTK/gmsh
# config) do not match and survive.
_SECRET_ENV_RE = re.compile(
    r"(API_KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE|"
    r"DEEPSEEK|DEEPINFRA|FIREWORKS|KIMI|LANGFUSE|MINIO|POSTGRES|REDIS|"
    r"MESH_API_KEY|DATABASE_URL|DSN|USER_TOKEN)",
    re.IGNORECASE,
)

# Modules a geometry/meshing script never needs - network/exfiltration, plus
# process-spawning and dynamic-import/FFI vectors that could escape the static scan.
_DENIED_IMPORTS = {
    # network / exfiltration
    "socket", "ssl", "http", "urllib", "urllib2", "urllib3", "requests",
    "httpx", "ftplib", "smtplib", "telnetlib", "asyncio", "aiohttp",
    "paramiko", "pycurl", "websocket", "websockets", "xmlrpc",
    # process spawning / shell
    "subprocess", "multiprocessing", "pty", "ptyprocess",
    # dynamic import / FFI (would let denied behaviour slip past this scan)
    "importlib", "ctypes", "cffi",
}

# Builtins that enable dynamic code/import (eval'd strings defeat a static scan).
_DENIED_CALLS = {"__import__", "eval", "exec", "compile"}
# os attributes that spawn shells / processes.
_DENIED_OS_ATTRS = {"system", "popen", "fork", "forkpty", "spawn", "spawnl",
                    "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
                    "spawnvp", "spawnvpe", "execl", "execle", "execlp",
                    "execlpe", "execv", "execve", "execvp", "execvpe"}


def scrubbed_subprocess_env(extra: dict | None = None) -> dict:
    base = {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}
    if extra:
        base.update(extra)
    return base


def scan_python_source(source: str, *, filename: str = "<model-code>") -> str | None:
    try:
        tree = ast.parse(source, filename=filename)
    except Exception as exc:
        logger.warning("safe_exec: could not AST-parse %s (%s) - skipping scan", filename, exc)
        return None

    denied: set[str] = set()
    env_access = False
    bad_calls: set[str] = set()
    shell_attrs: set[str] = set()
    dynamic_getattr = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _DENIED_IMPORTS:
                    denied.add(root)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _DENIED_IMPORTS:
                denied.add(root)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                if node.attr in ("environ", "getenv"):
                    env_access = True
                elif node.attr in _DENIED_OS_ATTRS:
                    shell_attrs.add(f"os.{node.attr}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id in _DENIED_CALLS:
                    bad_calls.add(fn.id)
                # getattr(os, "...") / getattr(__builtins__, "...") - dynamic escape
                elif fn.id == "getattr" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Name) and first.id in ("os", "__builtins__", "sys"):
                        dynamic_getattr = True
    problems = []
    if denied:
        problems.append(f"forbidden imports {sorted(denied)} (network/subprocess/dynamic-import)")
    if shell_attrs:
        problems.append(f"process/shell spawn {sorted(shell_attrs)}")
    if bad_calls:
        problems.append(f"dynamic code/import builtins {sorted(bad_calls)}")
    if dynamic_getattr:
        problems.append("dynamic getattr(os/sys/__builtins__, ...) (scan-evasion vector)")
    if env_access:
        problems.append("os.environ / os.getenv access (credential exfiltration risk)")
    return "; ".join(problems) if problems else None
