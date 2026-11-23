# Responsibility: The three gmsh members of the mesher panel and the translation of a worker's
#                 answer into a MemberResult.
# Owns: GmshProd, GmshDefault, GmshAlt and `result_from_call`.
# Boundaries: no gmsh here - each member is a name and a profile; the meshing runs in the worker
#             kernel function `mesh_gmsh` (`mesh_kernel`), where the profiles are defined.
"""gmsh as three different meshers: Hexera's production FEA settings, gmsh with nothing set, and gmsh
with another 2D/3D algorithm pair and its own element size. A file that meshes on one and not the
others is the failure the panel exists to find."""

from __future__ import annotations

from meshpipeline.cad.repair_v4.verdict.mesh_kernel import profile_config
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND, Located, MemberResult
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool


def result_from_call(name: str, call: CallResult) -> MemberResult:
    """A worker's answer as the member's result. A timeout or crash is the mesher failing on this
    file; any other kernel failure is infrastructure and raises `CheckerUnavailable`."""
    if call.status == "timeout":  # the mesher overran the limit: the file failed the definition
        return MemberResult(name, "timeout", "timeout", call.error or "", [], None, 0, call.seconds)
    if call.status == "crashed":
        # gmsh runs inside a pool worker, so an abort in gmsh and a pool death (OOM kill, replaced
        # worker, no worker to spawn) are the same EOF to the pool and cannot be told apart. The
        # reading that does not blame the file: infrastructure, so the verdict is inconclusive.
        return MemberResult(name, "crashed", INFRASTRUCTURE_KIND, call.error or "", [], None, 0, call.seconds)
    if not call.ok or call.value is None:
        raise CheckerUnavailable(f"{name}: mesh_gmsh: {call.status}: {call.error}")
    value = call.value
    located = [
        Located(item["entity"], None if item["xyz"] is None else tuple(item["xyz"]), item["mesher_tag"])
        for item in value["located"]
    ]
    return MemberResult(
        name,
        value["status"],
        value["error_kind"],
        value["message"],
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
        call = pool.call(self.fn_name, {"step": str(step_path), "profile": self.profile}, timeout_s=timeout_s)
        return result_from_call(self.name, call)


class GmshProd(GmshMember):
    """The options Hexera's FEA mesh runs with (`engines/fea_mesh_config.options`), on the part in metres
    as the adapter reads it: HXT fill, a tenth of the diagonal as the element size."""

    name = "gmsh_prod"
    profile = "prod"


class GmshDefault(GmshMember):
    """gmsh with no size or algorithm setting at all: what a customer who just opens the file in gmsh gets."""

    name = "gmsh_default"
    profile = "default"


class GmshAlt(GmshMember):
    """2D Frontal-Delaunay and 3D Delaunay (not HXT) at a twentieth of the diagonal: another fill
    algorithm and a finer mesh than the production member."""

    name = "gmsh_alt"
    profile = "alt"
