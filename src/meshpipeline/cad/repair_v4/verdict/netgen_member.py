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
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.verdict import panel, thin_sizing
from meshpipeline.cad.repair_v4.verdict.mesh_child import MIN_SICN
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND, Located, MemberResult
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool

NETGEN_PYTHON = "/opt/netgen-env/bin/python"
SCRIPT = Path(__file__).with_name("netgen_occ_script.py")
RESULT_PREFIX = "NETGEN_RESULT "
#: Netgen's element size is the bounding-box diagonal over this (the script's `maxh`), and touching
#: solids are Glued before meshing; both are handed to the script so `NetgenOCC.config` cannot drift from them.
MAXH_DIVISOR = 10.0
GLUE_TOUCHING_SOLIDS = True
#: The wheels in /opt/netgen-env (requirements/netgen.txt, which a test holds this equal to). The app
#: interpreter has no netgen, so `mesher_versions` cannot see this one: the pin is its version key.
NETGEN_PIN = {"netgen-mesher": "6.2.2608", "netgen-occt": "7.8.1"}
_TET = 4  # gmsh element type

Status = Literal["pass", "fail", "timeout", "crashed"]


def netgen_python() -> str | None:
    if Path(NETGEN_PYTHON).exists():
        return NETGEN_PYTHON
    return shutil.which("netgen-python")


def netgen_block(sizing: dict) -> dict:
    """The Netgen-side settings of the local sizing (thin_sizing): close edges, curvature safety, the floor (mm)."""
    block: dict = {"closeedgefac": thin_sizing.NETGEN_CLOSEEDGEFAC,
                   "curvaturesafety": thin_sizing.NETGEN_CURVATURESAFETY}
    if sizing.get("floor_mm"):
        block["minh"] = float(sizing["floor_mm"])
    return block


def _write_sizing(step: str, work: Path, timeout_s: float) -> str:
    """The part's thin-wall plan (measured once per file, thin_sizing.plan_for) with the Netgen block, as a JSON file
    the script reads."""
    sizing = thin_sizing.plan_for(step, min(thin_sizing.MEASURE_TIMEOUT_S, 0.5 * timeout_s))
    path = work / "sizing.json"
    path.write_text(json.dumps({**sizing, "netgen": netgen_block(sizing)}, sort_keys=True))
    return str(path)


def _run_netgen(step: str, out_msh: str, timeout_s: float, sizing_path: str | None = None) -> dict:
    glue = "glue" if GLUE_TOUCHING_SOLIDS else "noglue"
    python = netgen_python()
    if python is None:
        return {"ok": False, "stage": "unavailable", "error": "no netgen interpreter (mesh image only)"}
    try:
        proc = subprocess.run(
            [python, str(SCRIPT), step, out_msh, repr(MAXH_DIVISOR), glue, *([sizing_path] if sizing_path else [])],
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
            "returncode": proc.returncode,  # negative = killed by that signal (a segfault is -11)
            "error": f"Netgen exited {proc.returncode} without a result: {tail}",
        }
    try:
        parsed: dict = json.loads(lines[-1][len(RESULT_PREFIX) :])
    except ValueError as exc:  # a truncated line means Netgen died mid-write: the mesher's crash
        return {"ok": False, "stage": "crash", "returncode": proc.returncode, "error": f"unparseable Netgen result: {exc}"}
    return parsed


def _measure(msh: str) -> dict:
    """Re-read the Netgen volume mesh into gmsh: tet count, minSICN, free (boundary) faces.

    `free_faces` is DIAGNOSTIC ONLY and is not in the pass criterion: a tet face used once is
    boundary in a conformal mesh and in a non-conformal one alike, so the count alone cannot tell
    them apart (the native test checks WHERE they lie, which needs the geometry). Conformity is
    delivered by Glue in the script and held by that test.
    """
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


def _read_no_solids(record: dict) -> bool:
    """The script's own "no solids" read failure (`netgen_occ_script.main`), and nothing else."""
    return (not record.get("ok") and record.get("stage") == "read" and record.get("solids") == 0)


def _root_count(step: str) -> int:
    """How many transferable roots the STEP file has (0 when it cannot be read this way)."""
    try:
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader

        reader = STEPControl_Reader()
        return int(reader.NbRootsForTransfer()) if reader.ReadFile(step) == IFSelect_RetDone else 0
    except Exception:  # noqa: BLE001
        return 0


def _all_roots_brep(step: str, work: Path) -> str | None:
    """Every root of `step`, as STEPControl transfers them (one compound when there are several), written as a BREP
    Netgen reads whole. None when the file cannot be read this way either (the original verdict then stands)."""
    try:
        from OCP.BRepTools import BRepTools
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader

        reader = STEPControl_Reader()
        if reader.ReadFile(step) != IFSelect_RetDone:
            return None
        reader.TransferRoots()
        shape = reader.OneShape()
        if shape.IsNull():
            return None
        out = str(work / "all_roots.brep")
        return out if BRepTools.Write_s(shape, out) else None
    except Exception:  # noqa: BLE001 - the fallback is best effort; the first read's verdict stands
        return None


@register("mesh_netgen")
def _mesh_netgen(payload: dict) -> dict:
    """payload: step, timeout_s, optional out_dir (keeps the .msh there). Returns a record; failures are in it. The only way out is the worker dying."""
    out_dir = payload.get("out_dir")
    work = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="netgen_"))
    work.mkdir(parents=True, exist_ok=True)
    msh = str(work / "netgen_volume.msh")
    try:
        started = time.monotonic()
        step = str(payload["step"])
        # Netgen's STEP reader keeps only the FIRST root (D19/D20, measured on 92 multi-root files: it meshed one body
        # of up to 50). Every multi-root file is handed over as ONE BREP compound of all its roots, so Netgen judges
        # the whole part; a single-root file goes in exactly as before.
        whole = _all_roots_brep(step, work) if _root_count(step) > 1 else None
        # owner decision 2026-11-26: thin walls sized (thin_sizing); the measuring time comes out of Netgen's clock
        sizing_path = _write_sizing(step, work, float(payload["timeout_s"]))
        budget = max(1.0, float(payload["timeout_s"]) - (time.monotonic() - started))
        record = _run_netgen(whole or step, msh, budget, sizing_path)
        if whole is not None:
            record = {**record, "read_via": "all_roots_brep"}
        elif _read_no_solids(record):
            # Netgen's STEP reader keeps only the FIRST root of the file (lane C12, measured on 0053_00530659: roots
            # [SHELL, SOLID, SOLID, SOLID] -> Netgen sees one SHELL, 0 solids, while STEPControl and read_step_xcaf
            # see all four). Read every root here, write them as one BREP compound, and give Netgen that. Only on
            # this one failure, so every file Netgen already reads is meshed exactly as before.
            brep = _all_roots_brep(str(payload["step"]), work)
            remaining = float(payload["timeout_s"]) - (time.monotonic() - started)
            if brep is not None and remaining > 1.0:
                retry = _run_netgen(brep, msh, remaining, sizing_path)
                record = {**retry, "read_via": "all_roots_brep", "first_root_read": record}
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


def timed_out(call: CallResult) -> bool:
    """The pool's clock fired, or Netgen's own (the kernel function reports that one inside an `ok` call)."""
    return call.status == "timeout" or (
        call.ok and call.value is not None and call.value.get("stage") == "timeout" and not call.value.get("ok")
    )


def killed(call: CallResult) -> bool:
    """The Netgen subprocess was SIGKILLed (reported inside an `ok` call as a crash with returncode -9)."""
    return bool(
        call.ok and call.value is not None and call.value.get("stage") == "crash"
        and call.value.get("returncode") == -int(signal.SIGKILL)
    )


class NetgenOCC:
    """The panel's non-gmsh member: Netgen meshing the B-rep through its OCC interface."""

    name = "netgen_occ"

    @property
    def config(self) -> dict:
        """Everything that decides Netgen's answer, JSON-able, the same shape as the gmsh members'
        (`mesh_child.profile_config`: `profile`, `min_sicn`, then the mesher's own settings). Constants
        only: no paths, ids or clocks, and nothing ordered by a dict's history (the cache hash sorts keys)."""
        return {
            "profile": "netgen_occ",
            "min_sicn": MIN_SICN,
            "versions": dict(sorted(NETGEN_PIN.items())),
            "glue": GLUE_TOUCHING_SOLIDS,
            "sizing": {"maxh_divisor": MAXH_DIVISOR, "maxh": "diagonal / maxh_divisor"},
            "local_sizing": thin_sizing.config(),
        }

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult:
        def payload(budget_s: float) -> dict:
            return {"step": str(step_path), "timeout_s": panel.inner_timeout_s(budget_s)}

        retried = panel.call_with_timeout_retry(
            pool, "mesh_netgen", payload, timeout_s=timeout_s, timed_out=timed_out, killed=killed
        )
        call = retried.call

        def failed(status: Status, kind: str, cause: str) -> MemberResult:
            return MemberResult(
                name=self.name, status=status, error_kind=kind, message=call.error or "", seconds=call.seconds, cause=cause
            )

        if call.status == "timeout":
            return failed(
                "timeout", panel.timeout_kind(retried.timeouts, "netgen_timeout"),
                panel.timeout_cause(retried.timeouts, retried.skipped),
            )
        # The POOL failing under the mesher is not a verdict on the file; a Netgen crash on the
        # geometry is, see member_result.
        if call.status == "crashed":
            return failed("crashed", INFRASTRUCTURE_KIND, "pool_worker")
        if not call.ok or call.value is None:
            return failed("crashed", INFRASTRUCTURE_KIND, "kernel_call")
        return panel.classify_after_timeout(
            member_result(self.name, call.value, call.seconds, retried.timeouts, retried.kills, retried.skipped),
            retried.timeouts, "netgen_timeout",
        )


def _face_located(rec: dict) -> list:
    """The face Netgen named in its failure, by the box fingerprint gmsh's member uses (`mesh_child._located`), which
    `sites.find_sites` translates to the live face. Netgen's face numbering is its own; the box is not."""
    from meshpipeline.cad.repair.identity import fingerprint_of, quantum_for

    box, diag = rec.get("face_box"), rec.get("diag")
    if not isinstance(box, list) or len(box) != 6 or not diag:
        return []
    entity = fingerprint_of("face", {"bbox_min": box[:3], "bbox_max": box[3:]}, quantum=quantum_for(float(diag)))
    return [Located(entity, None, f"netgen face {rec.get('face')}", tuple(float(v) for v in box))] if entity else []


def member_result(
    name: str, rec: dict, seconds: float, timeouts: int = 0, kills: int = 0, skipped: bool = False
) -> MemberResult:
    """Turn a `mesh_netgen` record into the member's verdict.

    Vocabulary: `fail` = Netgen ran and the file does not mesh or meshes badly; `crashed` with
    `netgen_crash` = Netgen itself died on this geometry (a vote against the file, like a gmsh
    abort); `crashed` with `infrastructure` = the toolchain was absent or broken, which says
    nothing about the file. `timeouts` / `kills` / `skipped` are as in `panel.RetriedCall`: one timeout
    or SIGKILL is infrastructure and only a repeat is the file's.
    """

    def result(
        status: Status, kind: str | None, message: str, *, sicn: float | None = None, elements: int = 0,
        cause: str | None = None, located: list | None = None,
    ) -> MemberResult:
        if rec.get("read_via") == "all_roots_brep":
            message += " [read from every STEP root]"
        return MemberResult(
            name=name, status=status, error_kind=kind, message=message, located=located or [],
            min_sicn=sicn, elements=elements, seconds=seconds, cause=cause,
        )

    if not rec.get("ok"):
        stage = rec.get("stage", "")
        message = str(rec.get("error", "Netgen failed"))
        if stage == "timeout":
            return result(
                "timeout", panel.timeout_kind(timeouts, "netgen_timeout"), message,
                cause=panel.timeout_cause(timeouts, skipped),
            )
        if stage == "crash":
            code = rec.get("returncode")
            if code == -int(signal.SIGKILL):
                kind, cause = panel.sigkill_verdict(kills, skipped)
                return result("crashed", kind if kind == INFRASTRUCTURE_KIND else "netgen_crash", message, cause=cause)
            if isinstance(code, int) and code < 0:
                kind, cause = panel.signal_verdict(-code)
                return result("crashed", kind if kind == INFRASTRUCTURE_KIND else "netgen_crash", message, cause=cause)
            return result("crashed", "netgen_crash", message, cause=f"exit_{code}")
        if stage in ("unavailable", "import"):
            return result("crashed", INFRASTRUCTURE_KIND, message, cause="toolchain_absent")
        return result("fail", _FAIL_KINDS.get(stage, "netgen_failed"), message, located=_face_located(rec))
    tets, sicn = int(rec["tets"]), rec.get("min_sicn")
    if tets == 0 or sicn is None:
        return result("fail", "no_tets", "Netgen mesh has no tetrahedra")
    if sicn < MIN_SICN:
        return result(
            "fail", "low_quality",
            f"minSICN {sicn:.4g} is below {MIN_SICN} ({tets} tets)",
            sicn=float(sicn), elements=tets,
        )
    note = f"{tets} tets, {rec['solids']} solid(s){', glued' if rec.get('glued') else ''}"
    return result("pass", None, note, sicn=float(sicn), elements=tets)


__all__ = ["GLUE_TOUCHING_SOLIDS", "MAXH_DIVISOR", "NETGEN_PIN", "NetgenOCC", "killed", "member_result", "netgen_python", "timed_out"]
