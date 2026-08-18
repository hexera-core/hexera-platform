# Responsibility: Jail model-authored code: no network, no filesystem beyond its workspace, bounded resources.
# Owns: the seccomp filter, the Landlock ruleset including the interpreter's own runtime roots, and the rlimits.
# Boundaries: it fails CLOSED - if the jail cannot be applied the code does not run.
# Collaborates with: agents/builder/tools/research.py, which is its only caller.
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import resource
import struct
import sys

logger = logging.getLogger(__name__)

# Generous limits: bound runaway without tripping numpy/geometry math.
# NOTE: RLIMIT_NPROC is deliberately NOT set - it counts threads, and OpenBLAS/numpy
# spawn per-core worker threads that would fail under it. Fork-bomb protection lives
# at the container layer instead (docker-compose `pids_limit` on the worker), which
# is the correct place for it.
_RLIMIT_AS_BYTES    = 6 * 1024 ** 3     # 6 GB virtual address space (numpy-safe)
_RLIMIT_CPU_SECS    = 30                # CPU seconds (separate from the wall timeout)
_RLIMIT_FSIZE_BYTES = 256 * 1024 ** 2   # 256 MB max single-file write

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP      = 22
_SECCOMP_MODE_FILTER = 2
_AUDIT_ARCH_X86_64   = 0xC000003E
_NR_socket_x86_64    = 41
_RET_ALLOW       = 0x7FFF0000
_RET_ERRNO_EPERM = 0x00050001   # SECCOMP_RET_ERRNO | EPERM(1)
_RET_KILL_THREAD = 0x00000000


def _build_net_block_fprog():
    def stmt(code, k):        return struct.pack("HBBI", code, 0, 0, k)
    def jmp(code, k, jt, jf): return struct.pack("HBBI", code, jt, jf, k)
    prog = b"".join([
        stmt(0x20, 4),                        # A = seccomp_data.arch
        jmp(0x15, _AUDIT_ARCH_X86_64, 1, 0),  # if arch == x86_64 → skip the kill
        stmt(0x06, _RET_KILL_THREAD),         # wrong arch → kill
        stmt(0x20, 0),                        # A = seccomp_data.nr
        jmp(0x15, _NR_socket_x86_64, 0, 1),   # if nr == socket → deny, else allow
        stmt(0x06, _RET_ERRNO_EPERM),         # deny socket() with EPERM
        stmt(0x06, _RET_ALLOW),               # allow everything else
    ])
    buf = ctypes.create_string_buffer(prog, len(prog))

    class _SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    return _SockFprog(len(prog) // 8, ctypes.cast(buf, ctypes.c_void_p)), buf


_LIBC = None
_FPROG = None
_FPROG_BUF = None          # keep the BPF buffer alive for the process lifetime
_SECCOMP_READY = False
try:
    _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    _LIBC.prctl.restype = ctypes.c_int
    _LIBC.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    _FPROG, _FPROG_BUF = _build_net_block_fprog()
    _SECCOMP_READY = True
except Exception as exc:
    logger.error("sandbox_exec: seccomp net-block unavailable at import (%s) - "
                 "run_python will FAIL CLOSED unless RUN_PYTHON_REQUIRE_SANDBOX=false", exc)


# --- Landlock: unprivileged FILESYSTEM confinement (fixes cross-tenant reads) --- #
# Confines the child to its own workspace (rw) + read-only system paths the
# interpreter/numpy need, so model code cannot read SIBLING job workspaces or
# enumerate /srv/workspaces. Unprivileged (no namespaces/root); needs kernel ≥5.13.
_NR_landlock_create_ruleset = 444
_NR_landlock_add_rule       = 445
_NR_landlock_restrict_self  = 446
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_CREATE_RULESET_VERSION = 1
_FS_EXECUTE   = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR  = 1 << 3
_FS_ALL_ABI1  = (1 << 13) - 1                       # bits 0..12 (ABI≥1)
_FS_RO        = _FS_READ_FILE | _FS_READ_DIR | _FS_EXECUTE
#: `/opt` is NOT a system root. It is where applications get installed, so granting it wholesale
#: hands the jailed child every deployment that lives there - including this one's own tree and
#: `.env`, whenever the application is installed under /opt rather than a home directory. The only
#: thing under /opt this runtime needs is the isolated VMTK environment, so that is what is named.
_VMTK_PREFIX = "/opt/vmtk-env"
_RO_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", _VMTK_PREFIX, "/proc")
_RW_PATHS = ("/dev",)                               # /dev/null, /dev/urandom, …


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                              # different drives / relative input
        return False


def _runtime_ro_roots() -> tuple[str, ...]:
    import sysconfig

    candidates = [
        sys.prefix,                                 # the venv: pyvenv.cfg, bin/, site-packages
        sys.base_prefix,                            # the interpreter this venv was built from
        os.path.dirname(os.path.realpath(sys.executable)),
    ]
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            candidates.append(sysconfig.get_paths()[key])
        except KeyError:
            pass

    resolved: list[str] = []
    for raw in candidates:
        if not raw:
            continue
        real = os.path.realpath(raw)
        if not os.path.isdir(real):
            continue
        if any(_is_within(real, sysroot) for sysroot in _RO_PATHS):
            continue                                # already granted by the static system set
        resolved.append(real)

    # Keep only the outermost of any nested pair: granting `<venv>` makes
    # `<venv>/lib/python3.12/site-packages` redundant.
    minimal: list[str] = []
    for path in sorted(set(resolved), key=len):
        if not any(_is_within(path, kept) for kept in minimal):
            minimal.append(path)
    return tuple(minimal)


#: Read-only roots beyond the system set, for THIS interpreter. Empty for a system Python.
_RUNTIME_RO_PATHS = _runtime_ro_roots()


class _RsAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PbAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


_LANDLOCK_READY = False
_RS_ATTR = _RS_ATTR_PTR = None
_RS_ATTR_SIZE = 0
_SYS_PBS: list = []          # keep structs + fds alive for the process lifetime
_SYS_PB_PTRS: list = []
_WS_PB = _WS_PB_PTR = None

try:
    if _LIBC is not None:
        _LIBC.syscall.restype = ctypes.c_long
        _abi = _LIBC.syscall(_NR_landlock_create_ruleset, None, ctypes.c_size_t(0),
                             ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
        if _abi >= 1:
            _RS_ATTR = _RsAttr(_FS_ALL_ABI1)
            _RS_ATTR_PTR = ctypes.c_void_p(ctypes.addressof(_RS_ATTR))
            _RS_ATTR_SIZE = ctypes.sizeof(_RS_ATTR)
            for _p, _acc in ([(p, _FS_RO) for p in _RO_PATHS] +
                             [(p, _FS_RO) for p in _RUNTIME_RO_PATHS] +
                             [(p, _FS_READ_FILE | _FS_READ_DIR | _FS_WRITE_FILE) for p in _RW_PATHS]):
                try:
                    _fd = os.open(_p, os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY)
                except OSError:
                    continue
                _pb = _PbAttr(_acc, _fd)
                _SYS_PBS.append((_pb, _fd))
                _SYS_PB_PTRS.append(ctypes.c_void_p(ctypes.addressof(_pb)))
            _WS_PB = _PbAttr(_FS_ALL_ABI1, -1)
            _WS_PB_PTR = ctypes.c_void_p(ctypes.addressof(_WS_PB))
            _LANDLOCK_READY = True
        else:
            logger.warning("sandbox_exec: Landlock unavailable (abi=%s) - run_python keeps "
                           "the net+rlimit jail but NOT filesystem confinement", _abi)
except Exception as exc:
    logger.warning("sandbox_exec: Landlock setup failed (%s) - fs confinement off", exc)


def _apply_landlock(ws_fd: int) -> None:
    # only reachable when _LANDLOCK_READY (init succeeded) - narrow for mypy
    assert _LIBC is not None and _WS_PB is not None
    rs = _LIBC.syscall(_NR_landlock_create_ruleset, _RS_ATTR_PTR, _RS_ATTR_SIZE, 0)
    if rs < 0:
        raise OSError("landlock_create_ruleset failed")
    for ptr in _SYS_PB_PTRS:
        _LIBC.syscall(_NR_landlock_add_rule, rs, _LANDLOCK_RULE_PATH_BENEATH, ptr, 0)
    _WS_PB.parent_fd = ws_fd                          # mutate pre-built struct (no alloc)
    if _LIBC.syscall(_NR_landlock_add_rule, rs, _LANDLOCK_RULE_PATH_BENEATH, _WS_PB_PTR, 0) != 0:
        raise OSError("landlock_add_rule(workspace) failed")
    if _LIBC.syscall(_NR_landlock_restrict_self, rs, 0) != 0:
        raise OSError("landlock_restrict_self failed")
    os.close(rs)


def seccomp_supported() -> bool:
    return _SECCOMP_READY


def landlock_supported() -> bool:
    return _LANDLOCK_READY


def _apply_net_block() -> None:
    # only reachable when _SECCOMP_READY (init succeeded) - narrow for mypy
    assert _LIBC is not None and _FPROG is not None
    if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError("prctl(PR_SET_NO_NEW_PRIVS) failed")
    if _LIBC.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(_FPROG), 0, 0) != 0:
        raise OSError("prctl(PR_SET_SECCOMP) failed")


def jail_preexec(*, require_seccomp: bool = True, workspace_fd: int | None = None):
    def _preexec():
        for what, lim in (
            (resource.RLIMIT_AS, _RLIMIT_AS_BYTES),
            (resource.RLIMIT_CPU, _RLIMIT_CPU_SECS),
            (resource.RLIMIT_FSIZE, _RLIMIT_FSIZE_BYTES),
        ):
            try:
                resource.setrlimit(what, (lim, lim))
            except Exception:
                pass  # best-effort; the seccomp/landlock controls below are critical
        # Landlock needs NO_NEW_PRIVS (so does seccomp); set it once up front.
        _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        # Filesystem confinement - confine to the job workspace + RO system paths.
        if _LANDLOCK_READY and workspace_fd is not None:
            try:
                _apply_landlock(workspace_fd)
            except Exception:
                if require_seccomp:
                    os._exit(127)
        # Network block (the critical, kernel-enforced control).
        if _SECCOMP_READY:
            try:
                _apply_net_block()
                return
            except Exception:
                pass
        if require_seccomp:
            os._exit(127)  # could not jail → do not run the model code
    return _preexec
