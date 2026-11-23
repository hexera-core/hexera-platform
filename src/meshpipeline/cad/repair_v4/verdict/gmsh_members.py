# Responsibility: The three gmsh members of the mesher panel and the translation of a worker's
#                 answer into a MemberResult.
# Owns: GmshProd, GmshDefault, GmshAlt and `result_from_call`.
# Boundaries: no gmsh here - each member is a name and a profile; the meshing runs in the worker
#             kernel function `mesh_gmsh` (`mesh_kernel`), where the profiles are defined.
"""gmsh as three different meshers: Hexera's production FEA settings, gmsh with nothing set, and gmsh
with a 2D/3D algorithm pair neither of those uses, at its own element size. A file that meshes on one
and not the others is the failure the panel exists to find; `mesh_child.algorithms` is the pair each runs.
Each runs in a child process (`mesh_kernel`), so a gmsh crash is a vote against the file."""

from __future__ import annotations

from meshpipeline.cad.repair_v4.verdict.mesh_kernel import profile_config
from meshpipeline.cad.repair_v4.verdict.panel import (
    INFRASTRUCTURE_KIND,
    Located,
    MemberResult,
    call_with_timeout_retry,
    inner_timeout_s,
    timeout_kind,
)
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool


def timed_out(call: CallResult) -> bool:
    """The pool's clock fired, or gmsh's own (the kernel function reports that one inside an `ok` call)."""
    return call.status == "timeout" or (call.ok and call.value is not None and call.value.get("status") == "timeout")


def result_from_call(name: str, call: CallResult, timeouts: int = 0) -> MemberResult:
    """A worker's answer as the member's result; `timeouts` is how many of the attempts timed out.

    A timeout blames the file only when it repeated at twice the budget (`panel.timeout_kind`); one
    is infrastructure. A pool-level crash (worker died, none to spawn) is infrastructure too; gmsh dying
    is not that - it is a child process whose exit the kernel function classified (`mesh_kernel`) and
    arrives here as an `ok` call whose value says `crashed`. Any other kernel failure raises
    `CheckerUnavailable`."""
    if call.status == "timeout":
        return MemberResult(
            name, "timeout", timeout_kind(timeouts), call.error or "", [], None, 0, call.seconds,
            "timeout_once" if timeouts < 2 else "timeout_twice",
        )
    if call.status == "crashed":
        return MemberResult(
            name, "crashed", INFRASTRUCTURE_KIND, call.error or "", [], None, 0, call.seconds, "pool_worker"
        )
    if not call.ok or call.value is None:
        raise CheckerUnavailable(f"{name}: mesh_gmsh: {call.status}: {call.error}")
    value = call.value
    if value["status"] == "timeout":
        return MemberResult(
            name, "timeout", timeout_kind(timeouts), value["message"], [], None, 0, call.seconds,
            "timeout_once" if timeouts < 2 else "timeout_twice",
        )
    if value["status"] == "crashed":
        return MemberResult(
            name, "crashed", value["error_kind"], value["message"], [], None, 0, call.seconds, value.get("cause")
        )
    located = [
        Located(item["entity"], None if item["xyz"] is None else tuple(item["xyz"]), item["mesher_tag"])
        for item in value["located"]
    ]
    return MemberResult(
        name,
        value["status"],
        value["error_kind"],
        ("re-run at twice the budget after a timeout. " if timeouts == 1 else "") + value["message"],
        located,
        value["min_sicn"],
        int(value["elements"]),
        call.seconds,
    )


class GmshMember:
    name: str
    profile: str
    fn_name = "mesh_gmsh"  # the kernel function; tests substitute one that aborts or hangs

    @property
    def config(self) -> dict:
        return profile_config(self.profile)

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult:
        def payload(budget_s: float) -> dict:
            return {"step": str(step_path), "profile": self.profile, "timeout_s": inner_timeout_s(budget_s)}

        retried = call_with_timeout_retry(pool, self.fn_name, payload, timeout_s=timeout_s, timed_out=timed_out)
        return result_from_call(self.name, retried.call, retried.timeouts)


class GmshProd(GmshMember):
    """The options Hexera's FEA mesh runs with (`engines/fea_mesh_config.options`), on the part in metres
    as the adapter reads it: 2D Frontal-Delaunay (gmsh's own), 3D HXT, a tenth of the diagonal.

    This is the DEFAULT plan only: no zones, size fields, hints, Delaunay fallback pass or thread
    counts. Only what `fea_mesh_config` holds is shared with the adapter; the rest can drift."""

    name = "gmsh_prod"
    profile = "prod"


class GmshDefault(GmshMember):
    """gmsh with no size or algorithm setting at all: what a customer who just opens the file in gmsh gets."""

    name = "gmsh_default"
    profile = "default"


class GmshAlt(GmshMember):
    """MeshAdapt in 2D and the Frontal algorithm in 3D, neither of which prod or default uses, at a
    twentieth of the diagonal (`ALT_SIZE_DIVISOR` = 20, in millimetres): another algorithm pair and a
    finer mesh than the production member."""

    name = "gmsh_alt"
    profile = "alt"
