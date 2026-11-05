# Responsibility: Tell a process that is still working from one that has stopped, by every sign it gives.
# Owns: reading a process tree's CPU time and memory, and the rule for when a watched run counts as alive.
# Boundaries: it observes and never stops anything; the watchers that act on it (safe_exec, the lease keeper) decide.
"""Is it still working?

A watched native run used to count as alive only while its log grew or a stage file changed. gmsh
meshes the curves of a big assembly one after another and prints nothing for each, so on dev-areen
(25 Oct) ox_cryo_ll, ox_mc_lid and hemy_mount_base were stopped as "stalled" after 15 quiet minutes
while gmsh was still computing at full speed.

So a run shows life in any of three ways, and it is stalled only when all three have been flat:
* its OUTPUT moved (a watched file grew or was rewritten);
* its process tree burned CPU (the run itself, and anything it started);
* its process tree's memory changed by a real amount.

CPU is read from /proc, so this is Linux only. Where /proc is missing the CPU and memory signs are
simply absent and the output sign works as before.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

#: A tree counts as working when, between two looks, it burned at least this share of ONE core over
#: the time between them (5 % of a core: 0.5 s of CPU in a 10 s poll). An idle interpreter, a process
#: blocked on a lock and a sleeping shell all stay far below it.
MIN_CPU_SHARE = 0.05
#: ...and never less than this much CPU in absolute terms, so one clock tick of bookkeeping between
#: two very close looks is not taken for work.
MIN_CPU_S = 0.02
#: A memory change counts only past this size: a working mesher allocates and frees in large blocks,
#: while an idle process's resident size wobbles by pages.
MIN_RSS_STEP_BYTES = 16 * 1024 * 1024

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
try:
    _PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
except (ValueError, OSError):
    _PAGE = 4096


@dataclass(frozen=True)
class TreeSample:
    """One look at a process tree: its total CPU seconds (user + system, every process in it), its
    resident memory in bytes, and how many processes it had."""
    cpu_s: float
    rss_bytes: int
    processes: int


def _stat(pid: int) -> tuple[int, float, int] | None:
    """(parent pid, CPU seconds, resident bytes) of one process from /proc/<pid>/stat."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("ascii", "replace")
    except OSError:
        return None
    # the command name sits in parentheses and may itself hold spaces or ')': split after the LAST
    rest = raw[raw.rfind(")") + 2:].split()
    try:
        ppid = int(rest[1])
        cpu = (int(rest[11]) + int(rest[12])) / float(_CLK_TCK)   # utime + stime
        rss = int(rest[21]) * _PAGE
    except (IndexError, ValueError):
        return None
    return ppid, cpu, rss


#: THE MEMORY GUARD (sandbox/safe_exec): a watched run is stopped, cleanly and as out of memory, once
#: what cannot be reclaimed on its machine passes this share of what the machine allows - before the
#: kernel's OOM killer ends it (and, on Cloud Run, the whole job with it). The rest is the runner's.
MEMORY_GUARD_SHARE = 0.92


def _read_int(path: str) -> int | None:
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def memory_in_use() -> tuple[int, int] | None:
    """(bytes in use that cannot be reclaimed, bytes the machine allows) for the container this runs
    in (cgroup v2, else v1: anonymous memory and tmpfs - a Cloud Run job's files live there - against
    its limit), else the host's (/proc/meminfo). None when neither can be read."""
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    stat_path = ("/sys/fs/cgroup/memory.stat" if Path("/sys/fs/cgroup/memory.stat").exists()
                 else "/sys/fs/cgroup/memory/memory.stat")
    meminfo: dict[str, int] = {}
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = ln.partition(":")
            meminfo[k.strip()] = int(v.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    host = meminfo.get("MemTotal")
    if limit and host and limit < host:
        try:
            stat = {}
            for ln in Path(stat_path).read_text().splitlines():
                k, _, v = ln.partition(" ")
                stat[k] = int(v)
        except (OSError, ValueError):
            return None
        used = stat.get("anon", stat.get("total_rss", 0)) + stat.get("shmem", stat.get("total_shmem", 0))
        return used, limit
    if host and "MemAvailable" in meminfo:
        return host - meminfo["MemAvailable"], host
    return None


def descendants(root_pid: int) -> list[int]:
    """Every process below `root_pid` (not itself), read from /proc; [] where it cannot be read."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    children: dict[int, list[int]] = {}
    for name in names:
        if name.isdigit():
            st = _stat(int(name))
            if st is not None:
                children.setdefault(st[0], []).append(int(name))
    out: list[int] = []
    stack = list(children.get(int(root_pid), ()))
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


def sample_tree(root_pid: int, *, exclude: Iterable[int] = ()) -> TreeSample | None:
    """CPU and memory of `root_pid` and every process below it, leaving out `exclude` and their
    descendants. None when /proc cannot be read or the root is gone."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    table: dict[int, tuple[int, float, int]] = {}
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    for name in names:
        if name.isdigit():
            st = _stat(int(name))
            if st is not None:
                table[int(name)] = st
    if root_pid not in table:
        return None
    skip = {int(p) for p in exclude}
    children: dict[int, list[int]] = {}
    for pid, (ppid, _cpu, _rss) in table.items():
        children.setdefault(ppid, []).append(pid)
    cpu, rss, n = 0.0, 0, 0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid in skip:
            continue
        seen.add(pid)
        _ppid, c, r = table[pid]
        cpu, rss, n = cpu + c, rss + r, n + 1
        stack.extend(children.get(pid, ()))
    return TreeSample(cpu_s=cpu, rss_bytes=rss, processes=n)


def _output_mark(paths: Iterable) -> tuple:
    """(size, mtime) of every watched file; a path with a wildcard watches every file it matches
    (a mesher's per-stage logs, log.*), so a new one appearing is output too."""
    import glob
    out = []
    for p in paths:
        names = (sorted(glob.glob(str(p))) if any(c in str(p) for c in "*?[") else [p])
        for name in names:
            try:
                st = Path(name).stat()
                out.append((str(name), st.st_size, st.st_mtime_ns))
            except OSError:
                out.append((str(name), -1, -1))
    return tuple(out)


class LivenessWatch:
    """Watches one run for signs of life. Call `poll()` at each look: it returns the sign it saw
    since the previous look - "output", "cpu" or "memory" - or None when there was none.

    `pid` is the run's process (its whole tree is read); `paths` are the files whose change is
    output (a log, a stage file). `exclude` leaves processes out of the tree - the watcher's own,
    when it is a child of what it watches."""

    def __init__(self, pid: int | None, paths: Iterable = (), *, exclude: Iterable[int] = (),
                 min_cpu_share: float = MIN_CPU_SHARE, min_cpu_s: float = MIN_CPU_S,
                 min_rss_step: int = MIN_RSS_STEP_BYTES,
                 clock: Callable[[], float] = time.monotonic,
                 sampler: Callable[..., TreeSample | None] = sample_tree) -> None:
        self.pid = pid
        self.paths = tuple(paths)
        self.exclude = tuple(exclude)
        self.min_cpu_share, self.min_cpu_s, self.min_rss_step = min_cpu_share, min_cpu_s, min_rss_step
        self._clock, self._sampler = clock, sampler
        self._t = clock()
        self._out = _output_mark(self.paths)
        self._tree = self._sample()
        #: the most CPU seconds the tree has been seen to have used (for the run's record)
        self.cpu_s = self._tree.cpu_s if self._tree is not None else 0.0

    def _sample(self) -> TreeSample | None:
        if self.pid is None:
            return None
        try:
            return self._sampler(self.pid, exclude=self.exclude)
        except Exception:  # noqa: BLE001 - a look that fails is no sign either way
            return None

    def poll(self) -> str | None:
        now = self._clock()
        dt, self._t = max(0.0, now - self._t), now
        out = _output_mark(self.paths)
        moved_output = out != self._out
        self._out = out
        tree = self._sample()
        prev, self._tree = self._tree, tree if tree is not None else self._tree
        sign = "output" if moved_output else None
        if tree is not None and prev is not None and sign is None:
            # a tree that lost a process can read less CPU than before: only growth is work
            burned = tree.cpu_s - prev.cpu_s
            if burned >= max(self.min_cpu_s, self.min_cpu_share * dt):
                sign = "cpu"
            elif abs(tree.rss_bytes - prev.rss_bytes) >= self.min_rss_step:
                sign = "memory"
        if tree is not None:
            self.cpu_s = max(self.cpu_s, tree.cpu_s)
        return sign


__all__ = ["MEMORY_GUARD_SHARE", "MIN_CPU_S", "MIN_CPU_SHARE", "MIN_RSS_STEP_BYTES", "LivenessWatch",
           "TreeSample", "descendants", "memory_in_use", "sample_tree"]
