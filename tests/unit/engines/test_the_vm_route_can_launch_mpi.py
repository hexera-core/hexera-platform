# Responsibility: Verify the big-mesh VM route hands the mesh container a TMPDIR it can actually
# write to, which is what Open MPI needs to launch a parallel mesh at all.
# Boundaries: reads the startup script this route sends as VM metadata; starts no VM and no mesher.
"""THE FIRST PARALLEL MESH ON THE VM ROUTE DIED BEFORE THE MESHER RAN A STEP.

A real customer cold plate, planned at 260,201,552 cells across two processes and sent to an
n2-highmem-32: `preparePar` wrote processor0 and processor1, and then

    A call to mkdir was unable to create the desired directory:
      Directory: /work/ompi.c0af5b6718c8.1000
      Error:     Permission denied
    ...
      orte_session_dir failed

and cartesianMesh exited 1 after 0s, 28.5 s into a VM that then deleted itself.

WHY IT HAD NEVER BEEN SEEN. `STARTUP_SCRIPT` runs as root and creates the workspace mount, then
starts the container with `-e TMPDIR=/work -v /mnt/stateful_partition/work:/work`. The container
runs as uid 1000, so the mount arrived root-owned 0755 and nothing in the container could write
to it. Python's tempfile hides exactly that: gettempdir() tests TMPDIR, finds it unwritable and
silently falls back to /tmp - so every SERIAL mesh this route has ever run looked fine, because
nothing else in the chain needs TMPDIR by name. Open MPI does not fall back. It puts its session
directory under TMPDIR and fails hard, so the defect waited for the first run that used MPI.

MEASURED BOTH WAYS on the mesh image, not reasoned about: a root-owned 0755 TMPDIR reproduces
that error exactly, and the same image with the same command on a 1777 one starts both ranks.
"""
from __future__ import annotations

import re

from meshpipeline.adapters.mesh_execution.gce_vm import STARTUP_SCRIPT

#: The directory the container is given as TMPDIR, as the docker run line sets it.
WORK = "/mnt/stateful_partition/work"


def _lines() -> list[str]:
    return [ln.strip() for ln in STARTUP_SCRIPT.splitlines() if ln.strip()
            and not ln.strip().startswith("#")]


def _index_of(pattern: str) -> int:
    for i, ln in enumerate(_lines()):
        if re.search(pattern, ln):
            return i
    raise AssertionError(f"the startup script has no line matching {pattern!r}")


def test_the_container_is_given_a_tmpdir_it_can_write_to():
    """The whole defect in one row: the mount is made writable, not left root-owned 0755."""
    assert re.search(rf"chmod\s+1777\s+{re.escape(WORK)}", STARTUP_SCRIPT), (
        "the TMPDIR mount has to be made writable by the container's user, or Open MPI cannot "
        "create its session directory and no parallel mesh can start"
    )


def test_it_is_made_writable_before_the_container_starts():
    """Order matters and a chmod after `docker run` would read as a fix while changing nothing -
    the run is in the foreground, so anything after it happens once the mesh is over."""
    assert _index_of(rf"chmod\s+1777\s+{re.escape(WORK)}") < _index_of(r"^docker run"), (
        "the chmod has to come before the container that depends on it"
    )


def test_the_directory_made_writable_is_the_one_handed_over_as_tmpdir():
    """And it is the same path on both sides. A chmod on a path the container never sees would
    pass the row above and fix nothing."""
    mount = re.search(r"-v\s+([^\s:]+):(\S+)", STARTUP_SCRIPT)
    tmpdir = re.search(r"-e\s+TMPDIR=(\S+)", STARTUP_SCRIPT)
    assert mount and tmpdir, "the run still has to mount a workspace and name it as TMPDIR"
    host_path, container_path = mount.group(1), mount.group(2)
    assert host_path == WORK, f"the mount source moved to {host_path}; this guard is stale"
    assert container_path == tmpdir.group(1), (
        f"TMPDIR is {tmpdir.group(1)} but the writable mount lands at {container_path}"
    )


def test_the_mount_is_still_created_before_it_is_made_writable():
    """The control for the row above: a chmod on a directory that does not exist yet fails, and
    `set -u` does not stop the script, so the container would start on the old broken mount."""
    assert _index_of(rf"mkdir -p .*{re.escape(WORK)}") < _index_of(
        rf"chmod\s+1777\s+{re.escape(WORK)}")
