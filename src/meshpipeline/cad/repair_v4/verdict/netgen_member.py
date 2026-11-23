# Responsibility: The Netgen panel member - does this STEP file mesh in a mesher that is not gmsh?
# Owns: `NetgenOCC` and the `mesh_netgen` kernel function. Netgen runs in its isolated interpreter
#       (netgen_occ_script.py); the quality floor is measured here, in gmsh, like every other member.
# Boundaries: the panel contract (PanelMember, MemberResult) is verdict/panel.py; the Netgen
#             interpreter is the mesh image's /opt/netgen-env, pinned by requirements/netgen.txt.
"""A clean file is mesher-agnostic, so the panel needs a member that is not gmsh.

Netgen reads the B-rep through its own OpenCASCADE (netgen-occt), which cannot share an interpreter
with the app's OCP and numpy, so the member runs it as a subprocess from inside a worker kernel
function. The volume mesh comes back as a Gmsh2 `.msh` and is re-read into gmsh to get `minSICN`, so
the floor is the same number the gmsh members report.

Solids that touch are Glued before meshing: unglued, each solid meshes alone and a pair that shares a
face passes as two meshes that do not meet.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.verdict.panel import MemberResult, WorkerPool

NETGEN_PYTHON = "/opt/netgen-env/bin/python"
SCRIPT = Path(__file__).with_name("netgen_occ_script.py")
RESULT_PREFIX = "NETGEN_RESULT "
#: the same pass floor every panel member holds a tet mesh to
MIN_SICN_PASS = 0.01
#: seconds the kernel function leaves the pool for gmsh and teardown after Netgen is stopped
_POOL_MARGIN_S = 30.0
_TET = 4  # gmsh element type

Status = Literal["pass", "fail", "timeout", "crashed"]


def netgen_python() -> str | None:
    if Path(NETGEN_PYTHON).exists():
        return NETGEN_PYTHON
    return shutil.which("netgen-python")


def _run_netgen(step: str, out_msh: str, timeout_s: float) -> dict:
    python = netgen_python()
    if python is None:
        return {"ok": False, "stage": "unavailable", "error": "no netgen interpreter (mesh image only)"}
    try:
        proc = subprocess.run(
            [python, str(SCRIPT), step, out_msh],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "timeout", "error": f"Netgen exceeded {timeout_s:.0f}s"}
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_PREFIX)]
    if proc.returncode != 0 or not lines:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        return {
            "ok": False,
            "stage": "crash",
            "error": f"Netgen interpreter exited {proc.returncode} without a result: {tail}",
        }
    parsed: dict = json.loads(lines[-1][len(RESULT_PREFIX):])
    return parsed


def _measure(msh: str) -> dict:
    """Re-read the Netgen volume mesh into gmsh: tet count, minSICN, free (boundary) faces."""
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(msh)
        _types, tags, _nodes = gmsh.model.mesh.getElements(3)
        tets = [int(t) for tt, ts in zip(_types, tags, strict=True) if tt == _TET for t in ts]
        if not tets:
            return {"tets": 0, "min_sicn": None, "free_faces": 0}
        quality = gmsh.model.mesh.getElementQualities(tets, "minSICN")
        faces = gmsh.model.mesh.getElementFaceNodes(_TET, 3)
        counts: dict[tuple[int, ...], int] = {}
        for i in range(0, len(faces), 3):
            key = tuple(sorted(int(n) for n in faces[i : i + 3]))
            counts[key] = counts.get(key, 0) + 1
        return {
            "tets": len(tets),
            "min_sicn": float(min(quality)),
            "free_faces": sum(1 for c in counts.values() if c == 1),
        }
    finally:
        gmsh.finalize()


@register("mesh_netgen")
def _mesh_netgen(payload: dict) -> dict:
    """payload: step, timeout_s, optional out_dir (keeps the .msh there). Returns a record, never raises."""
    out_dir = payload.get("out_dir")
    work = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="netgen_"))
    work.mkdir(parents=True, exist_ok=True)
    msh = str(work / "netgen_volume.msh")
    try:
        record = _run_netgen(str(payload["step"]), msh, float(payload["timeout_s"]))
        if not record.get("ok"):
            return record
        if not Path(msh).is_file():
            return {**record, "ok": False, "stage": "export", "error": "Netgen reported success but wrote no mesh"}
        try:
            measured = _measure(msh)
        except Exception as exc:
            return {**record, "ok": False, "stage": "measure", "error": f"gmsh could not read the Netgen mesh: {exc}"}
        return {**record, **measured, "msh": msh}
    finally:
        if not out_dir:
            shutil.rmtree(work, ignore_errors=True)


_FAIL_KINDS = {
    "read": "step_unreadable",
    "mesh": "netgen_meshing_failed",
    "export": "netgen_export_failed",
    "measure": "netgen_mesh_unreadable",
    "timeout": "netgen_timeout",
}


class NetgenOCC:
    """The panel's non-gmsh member: Netgen meshing the B-rep through its OCC interface."""

    name = "netgen_occ"

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult:
        start = time.monotonic()
        # Netgen gets the time the pool's own clock would otherwise spend killing the worker; the
        # subprocess is stopped first so its mesh stage is never orphaned by a worker SIGKILL.
        inner = max(1.0, timeout_s - _POOL_MARGIN_S) if timeout_s > 2 * _POOL_MARGIN_S else timeout_s * 0.8
        call = pool.call("mesh_netgen", {"step": str(step_path), "timeout_s": inner}, timeout_s=timeout_s)

        def result(
            status: Status, kind: str | None, message: str, *, sicn: float | None = None, elements: int = 0
        ) -> MemberResult:
            return MemberResult(
                name=self.name, status=status, error_kind=kind, message=message, located=[],
                min_sicn=sicn, elements=elements, seconds=time.monotonic() - start,
            )

        if call.status == "timeout":
            return result("timeout", "netgen_timeout", call.error or "worker timed out")
        if call.status == "crashed":
            return result("crashed", "worker_crashed", call.error or "worker crashed")
        if not call.ok or call.value is None:
            return result("crashed", "kernel_error", call.error or "kernel call failed")
        rec = call.value
        if not rec.get("ok"):
            stage = rec.get("stage", "")
            message = str(rec.get("error", "Netgen failed"))
            if stage == "timeout":
                return result("timeout", "netgen_timeout", message)
            if stage in ("unavailable", "import", "crash"):  # the toolchain's fault, not the file's
                return result("crashed", f"netgen_{stage}", message)
            return result("fail", _FAIL_KINDS.get(stage, "netgen_failed"), message)
        tets, sicn = int(rec["tets"]), rec.get("min_sicn")
        if tets == 0 or sicn is None:
            return result("fail", "no_tets", "Netgen mesh has no tetrahedra")
        if sicn < MIN_SICN_PASS:
            return result(
                "fail", "low_quality",
                f"minSICN {sicn:.4g} is below {MIN_SICN_PASS} ({tets} tets)",
                sicn=float(sicn), elements=tets,
            )
        note = f"{tets} tets, {rec['solids']} solid(s){', glued' if rec.get('glued') else ''}"
        return result("pass", None, note, sicn=float(sicn), elements=tets)


__all__ = ["MIN_SICN_PASS", "NetgenOCC", "netgen_python"]
