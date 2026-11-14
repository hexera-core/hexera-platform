# Responsibility: Stop a retry from running the mesher on exactly the case an earlier attempt of the same run already ran.
# Owns: the case digest (the authored spec plus the surfaces staged for the mesher) and the run's in-process ledger of judged cases.
# Boundaries: it reads workspace files and remembers digests; it decides nothing about a mesh and launches nothing.
# Collaborates with: agents/builder/tools/meshing.py (prepare refuses, execute remembers).
"""NO IDENTICAL RETRIES.

A retry that hands the mesher the same engine, the same authored case and the same staged
surfaces as an earlier attempt of the run gets the same result back - it only burns another
attempt and another 20 minutes (job d20ad762 ran an identical attempt twice). So before a retry's
mesher starts, its case is compared with every case an EARLIER attempt of this run already ran to a
verdict: when it is byte for byte one of them, the run is refused before the announcement, with
what that earlier run came to, and the builder must change something meaningful or stop.

Only a JUDGED run counts: one that came back with a mesh, a mesher failure or a timeout. A run our
infrastructure dropped, or one the last launch check refused, says nothing about the case, so the
same case may run again. Runs inside one attempt are the engine's own business (its pass logic and
the submission replay identity), so only another attempt's run is ever matched.

In-process by design, like the record of native runs: every attempt of a run is built by the same
worker, a resumed run starts a new generation directory, and nothing here may enter a workspace
whose bytes are the submission's identity.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Files the mesher reads besides the authored spec: the surfaces staged for it.
_SURFACE_SUFFIXES = frozenset({".stl", ".fms", ".ftr", ".vtp", ".vtk", ".obj", ".step", ".stp",
                               ".brep", ".iges", ".igs"})
#: Where a case's staged surfaces live, besides the workspace root.
_SURFACE_DIRS = ("constant/triSurface",)
#: Workspace facts the engines size the case from, beside the spec: the resolution ladder's rung
#: (engines/resolution_policy.LADDER_FILE). A retry one rung up hands the mesher a different case
#: even when the authored spec is unchanged - the rung IS the change (LAD-cfd-1).
_FACT_FILES = ("resolution_ladder.json",)

#: generation directory -> case digest -> {"attempt": the workspace's name, "outcome": words}
_LEDGER: dict[str, dict[str, dict]] = {}
_LEDGER_KEPT = 256


def case_digest(workspace: Path, required_files) -> str:
    """The case a mesher is about to be handed: the engine's authored spec files and every surface
    staged for it, by relative name and content. '' when there is nothing to compare."""
    ws = Path(workspace)
    files: dict[str, Path] = {}
    for rel in required_files or ():
        p = ws / rel
        if p.is_file():
            files[str(rel)] = p
    for p in ws.iterdir() if ws.is_dir() else ():
        if p.is_file() and p.suffix.lower() in _SURFACE_SUFFIXES:
            files.setdefault(p.name, p)
    for name in _FACT_FILES:
        if (ws / name).is_file():
            files.setdefault(name, ws / name)
    for d in _SURFACE_DIRS:
        root = ws / d
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    files.setdefault(p.relative_to(ws).as_posix(), p)
    if not files:
        return ""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8") + b"\0")
        h.update(_file_digest(files[rel]))
    return h.hexdigest()


def _file_digest(path: Path) -> bytes:
    # streamed: a staged surface can be hundreds of megabytes, and a whole-file read before every
    # run would hold it in memory for nothing
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


#: How the builder names an attempt's workspace inside its run's generation directory
#: (agents/builder/workspace._setup_workspace: <job>/generation_<g>/attempt_<n>). Only workspaces
#: laid out that way are attempts of one run; any other directory is never matched.
_ATTEMPT_PREFIX = "attempt_"


def _generation(workspace: Path) -> str:
    try:
        return str(Path(workspace).resolve().parent)
    except (OSError, RuntimeError):
        return str(Path(workspace).parent)


def _is_attempt(workspace: Path) -> bool:
    return Path(workspace).name.startswith(_ATTEMPT_PREFIX)


def earlier_identical(workspace: Path, digest: str) -> dict | None:
    """The earlier attempt of this run that ran exactly this case to a verdict, or None."""
    if not digest or not _is_attempt(workspace):
        return None
    seen = _LEDGER.get(_generation(workspace), {}).get(digest)
    if not seen or seen.get("attempt") == Path(workspace).name:
        return None
    return dict(seen)


def remember(workspace: Path, digest: str, result: dict) -> None:
    """Record what a run of this case came to - when it came to a verdict at all."""
    if not digest or not isinstance(result, dict) or not _is_attempt(workspace):
        return
    if result.get("system_failure") or result.get("patch_contract_mismatch"):
        return          # our infrastructure, or a launch refusal: nothing was learned about the case
    outcome = ("it ran out of time" if result.get("timed_out") else
               "it built a mesh that passed its checks" if result.get("success") else
               "its mesh did not pass its checks")
    gen = _generation(workspace)
    _LEDGER.setdefault(gen, {})[digest] = {"attempt": Path(workspace).name, "outcome": outcome}
    while len(_LEDGER) > _LEDGER_KEPT:
        _LEDGER.pop(next(iter(_LEDGER)))


def refusal(earlier: dict) -> dict:
    """The run_mesh result for an identical re-run: refused before any mesher starts."""
    where = str(earlier.get("attempt") or "an earlier attempt").replace("_", " ")
    return {
        "success": False, "identical_rerun": True, "error": "identical_rerun",
        "earlier_attempt": earlier.get("attempt"),
        "guidance": (f"Nothing was meshed: this exact case - the same spec and the same surfaces - "
                     f"already ran in {where}, and {earlier.get('outcome')}. Running it again can "
                     "only repeat that. Change something meaningful that answers the failure "
                     "(the refinement, the cell sizes, the layers), then run_mesh - or, if nothing "
                     "can, stop and say so.")}


# #
# THE SAME GUARD, FOR AN ENGINE THAT IS NOT DRIVEN BY THE MODEL
# #
# Everything above is reached from the builder's `run_mesh` TOOL, which only the model-led loop
# calls. An engine with a deterministic DRIVER - the conjugate mesher, the FEA driver, snapgrid -
# never passes through that tool, so it was never guarded at all; and for a driver the loss is
# larger than for the loop. The builder's own no-op detector compares the files an attempt
# AUTHORED, which for a driver is only knowable once the driver has run - and the driver runs the
# mesher. `NOOP_LIMIT = 2` then allows a second identical attempt before the budget is spent.
#
# MEASURED on the Corintis plate, 2026-11-12: attempts 2 and 3 authored the same spec as attempt
# 1 and were 80 minutes and two n2-highmem-32 hours of work that could not have come out
# differently. The pair below is bound by the composition root to `contracts/mesh_execution`'s
# launch check and run recorder - the one seam EVERY engine's native run passes - so the ledger
# is filled by every engine and read before every launch, and no engine has to remember either.
#
# WHAT IT CANNOT REFUSE, by the rules already written above: a run inside the SAME attempt (an
# engine's own passes, and the VM re-run of a Cloud Run pass that met the wall still working), a
# case no earlier attempt ran to a verdict, and anything our infrastructure dropped.

#: workspace -> the digest of the case its current run was launched with. The digest is taken
#: BEFORE the mesher starts and read back after it returns, because a run writes into its own
#: workspace (constant/polyMesh, logs, an exported surface) and a digest taken afterwards is not
#: the one the next attempt would be compared against.
_LAUNCHED: dict[str, str] = {}
_LAUNCHED_KEPT = 256


def _required_files(engine: str):
    from meshpipeline.agents.builder.tools import get_spec_run_files
    try:
        return get_spec_run_files(engine)
    except Exception:    # noqa: BLE001 - an engine with no run policy simply has no spec files
        return ()


def before_launch(workspace, engine: str) -> dict | None:
    """Refuse a native run whose case an earlier attempt of this run already ran to a verdict,
    shaped like any native result (contracts/mesh_execution.set_launch_check). None lets it go."""
    from meshpipeline.contracts.mesh_execution import RC_IDENTICAL_RERUN

    ws = Path(workspace)
    digest = case_digest(ws, _required_files(engine))
    _LAUNCHED[_key(ws)] = digest
    while len(_LAUNCHED) > _LAUNCHED_KEPT:
        _LAUNCHED.pop(next(iter(_LAUNCHED)))
    earlier = earlier_identical(ws, digest)
    if earlier is None:
        return None
    said = refusal(earlier)
    logger.error("rerun guard: the %s case in %s is byte for byte the one %s already ran (%s) - "
                 "refusing to launch the mesher", engine, workspace, earlier.get("attempt"),
                 earlier.get("outcome"))
    return {"rc": RC_IDENTICAL_RERUN, "timed_out": False,
            "log_tail": "[IDENTICAL_RERUN] " + said["guidance"],
            "identical_rerun": True, "earlier_attempt": earlier.get("attempt"),
            "guidance": said["guidance"]}


def after_run(workspace, engine: str, result: dict) -> None:
    """Record what the case this workspace launched came to (contracts/mesh_execution.
    set_run_recorder). The digest is the one taken at launch, never one re-read from a workspace
    the run has since written into."""
    remember(Path(workspace), _LAUNCHED.get(_key(workspace), ""), result)


def _key(workspace) -> str:
    try:
        return str(Path(workspace).resolve())
    except (OSError, RuntimeError, TypeError):
        return str(workspace)


__all__ = ["after_run", "before_launch", "case_digest", "earlier_identical", "refusal", "remember"]
