# Responsibility: Verify the big-mesh VM route sends what it knows about a failed run BEFORE it
# deletes the machine that knows it.
# Boundaries: reads the startup script this route sends as VM metadata; starts no VM and no mesher.
"""THE BOX DELETES ITSELF PASS OR FAIL, SO A FAILURE HAS ONE CHANCE TO SPEAK.

Job `110daca9-4bb2-4fef-a649-bd500b676e36` on 2026-11-24: an n2-highmem-64 meshed the real
Corintis cold plate for 1 h 53 m, the container stopped, and the instance was deleted six seconds
later - by its own startup script, which runs `docker run ...` and then issues the delete whatever
the run did. The 2 TB disk went with it. Afterwards the audit log showed the self-delete and
nothing else: no exit code, no `cartesianMesh.log`, no console. The run's entire account was the
waiting worker's inference from silence, "a n2-highmem-64 VM stopped answering".

The delete has to stay unconditional - a failure that left 64 cores and 512 GiB idle is the thing
`SELF-DELETE REFUSED` above it was written for. So the fix is not to keep the box: it is that the
box says what it knows first, and only when there is something to say.
"""
from __future__ import annotations

import re

from meshpipeline.adapters.mesh_execution.gce_vm import STARTUP_SCRIPT

WORK = "/mnt/stateful_partition/work"


def _lines() -> list[str]:
    return [ln.strip() for ln in STARTUP_SCRIPT.splitlines() if ln.strip()
            and not ln.strip().startswith("#")]


def _index_of(pattern: str) -> int:
    for i, ln in enumerate(_lines()):
        if re.search(pattern, ln):
            return i
    raise AssertionError(f"the startup script has no line matching {pattern!r}")


def test_the_mesher_s_own_output_is_kept_on_disk():
    """`docker run` is in the foreground and the container is `--rm`, so its output exists only as
    the startup script's stdout - on the serial console of a machine that is about to be deleted.
    It is teed, not redirected: the console still gets every line."""
    assert re.search(r"\|\s*tee\s+\"?\$CONSOLE\"?", STARTUP_SCRIPT), (
        "the container's output has to be kept somewhere a failed run can send it from"
    )
    assert re.search(r"^CONSOLE=\S+", STARTUP_SCRIPT, re.MULTILINE), "CONSOLE has to be set"


def test_the_kept_output_is_not_inside_the_workspace_the_mesh_sends_home():
    """A log under the mount would ride home inside the result archive, and would be read by the
    beacon as the mesher's own output moving."""
    console = re.search(r"^CONSOLE=(\S+)", STARTUP_SCRIPT, re.MULTILINE)
    assert console, "CONSOLE has to be set"
    assert not console.group(1).startswith(WORK + "/"), (
        f"{console.group(1)} is inside the workspace mount"
    )


def test_the_exit_code_is_the_mesher_s_and_not_the_tee_s():
    """`$?` after a pipeline is the LAST command's, and `tee` succeeds whatever the mesher did -
    so a failed mesh would read as a clean one and send nothing."""
    assert re.search(r"RC=\$\{PIPESTATUS\[0\]\}", STARTUP_SCRIPT), (
        "the mesher's exit code has to be taken from PIPESTATUS[0]"
    )
    assert not re.search(r"RC=\$\?", STARTUP_SCRIPT), (
        "$? after the pipeline is tee's exit code, which is always 0"
    )


def test_what_the_run_knew_is_sent_only_when_the_run_failed():
    """A run that worked pays nothing for this: its result is already home and its workspace is
    already in the archive."""
    guard = _index_of(r'if\s+\[\s+"\$RC"\s+-ne\s+0\s+\]')
    upload = _index_of(r"mesh_failure\.txt.*storage\.googleapis\.com|storage\.googleapis\.com.*mesh_failure")
    assert guard < upload, "the upload has to sit inside the failed-run branch"


def test_it_is_sent_before_the_box_deletes_itself():
    """Order is the whole point. Anything after the DELETE is on a machine that no longer exists."""
    upload = _index_of(r"--data-binary @/mnt/stateful_partition/mesh_failure\.txt")
    delete = _index_of(r"-X DELETE")
    assert upload < delete, (
        "what the run knew has to leave the machine before the machine does"
    )


def test_it_lands_beside_the_result_the_worker_was_waiting_for():
    """Same bucket and same job folder as output.tar.gz, derived from the run's own output-uri -
    never a second place a person has to be told about."""
    assert re.search(r"OUT=\$\(get instance/attributes/output-uri\)", STARTUP_SCRIPT)
    assert re.search(r"BKT=.*gs://", STARTUP_SCRIPT), "the bucket comes off the output uri"
    assert re.search(r"KEY=.*output-uri|KEY=\$\(echo \"\$OUT\"", STARTUP_SCRIPT), (
        "the job folder comes off the output uri"
    )
    assert "%2F" in STARTUP_SCRIPT, (
        "the object name goes in a query parameter, so its slashes have to be encoded or the "
        "upload lands under a truncated name"
    )


def test_the_three_things_a_dead_mesh_is_diagnosed_from_are_all_in_it():
    """What was actually missing on 110daca9, each named: the exit code, the stage logs, and
    whether the kernel took the process."""
    body = STARTUP_SCRIPT
    assert re.search(r"the mesher exited \$RC", body), "the exit code"
    assert re.search(rf"for f in {re.escape(WORK)}/\*/\*\.log", body), "every stage log"
    assert re.search(r"dmesg.*(out of memory|oom-kill)", body, re.IGNORECASE), (
        "an OOM kill and a crash look identical from outside the box"
    )
    assert re.search(r'tail -c \d+ "\$CONSOLE"', body), "the container's own last words"


def test_a_failed_upload_does_not_take_the_delete_with_it():
    """curl without -f, and no `set -e`: a refused upload must not leave the box running. The
    comment above the delete is about exactly this failure mode in the other direction."""
    assert not re.search(r"^set -e", STARTUP_SCRIPT, re.MULTILINE), (
        "set -e would make a failed diagnostics upload skip the delete and leak the machine"
    )
    upload_line = [ln for ln in _lines() if "mesh_failure.txt" in ln and "curl" in ln]
    assert upload_line, "the upload line has to be there"
    assert " -f " not in upload_line[0], (
        "curl -f exits non-zero on an HTTP error; the delete must still run"
    )
