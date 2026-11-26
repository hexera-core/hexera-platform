# Responsibility: Prove the Netgen panel member against the real Netgen, OCCT and gmsh in the mesh image.
# Boundaries: the native tier. Fixtures are authored here with OCP; the member under test runs in a
#             WorkerPool process and reaches Netgen through its isolated interpreter.
from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopoDS import TopoDS_Compound

from meshpipeline.cad.repair_v4.verdict.mesh_child import MIN_SICN
from meshpipeline.cad.repair_v4.verdict.netgen_member import NETGEN_PIN, NetgenOCC, netgen_python
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

TIMEOUT_S = 600  # emulated amd64 on arm64 is slow; a slow run is not a hang
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "geometry"
#: ABC 00500400, from devtools/validation/corpora/mesh-failure-real.json: gmsh says
#: "Invalid boundary mesh (overlapping facets)"; 1 solid, 8 faces.
OVERLAPPING_FACETS = FIXTURES / "abc_overlapping_facets_00500400.step"


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=120)
    try:
        yield p
    finally:
        p.close()


def _write(shapes: list, path: Path) -> str:
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for shape in shapes:
        builder.Add(comp, shape)
    writer = STEPControl_Writer()
    writer.Transfer(comp, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def _box(x: float) -> object:
    return BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 10.0, 10.0, 10.0).Shape()


@pytest.mark.native_smoke
def test_netgen_occ_interface_is_in_the_mesh_image() -> None:
    python = netgen_python()
    assert python is not None, "no netgen interpreter in this image"
    code = (
        "import netgen.occ, importlib.metadata as m;"
        "print({d: m.version(d) for d in ('netgen-mesher', 'netgen-occt')})"
    )
    done = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    # BOTH wheels, against the pin the cache key carries, not a literal that can drift from it
    assert ast.literal_eval(done.stdout.strip().splitlines()[-1]) == NETGEN_PIN


def test_box_passes(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write([_box(0)], tmp_path / "box.step")
    result = NetgenOCC().run(pool, step, timeout_s=TIMEOUT_S)
    assert result.name == "netgen_occ"
    assert result.status == "pass", result
    assert result.error_kind is None
    assert result.elements > 0
    assert result.min_sicn is not None and result.min_sicn >= MIN_SICN


def test_face_sharing_boxes_mesh_as_one_conformal_mesh(pool: WorkerPool, tmp_path: Path) -> None:
    """Two solids sharing the x=10 face must come out as ONE mesh whose interface is interior.

    The measure is mesher-independent: a tet face used by exactly one tet is on the boundary. In a
    conformal mesh none of those lie on the x=10 interface plane; two meshes that merely sit side
    by side leave a full set of free faces there.
    """
    import gmsh

    step = _write([_box(0), _box(10)], tmp_path / "two.step")
    out = tmp_path / "kept"
    call = pool.call("mesh_netgen", {"step": step, "timeout_s": TIMEOUT_S, "out_dir": str(out)}, timeout_s=TIMEOUT_S + 30)
    assert call.ok and call.value is not None, call
    assert call.value["ok"], call.value
    assert call.value["solids"] == 2 and call.value["glued"] is True

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(call.value["msh"])
        faces = gmsh.model.mesh.getElementFaceNodes(4, 3)
        node_ids, coords, _ = gmsh.model.mesh.getNodes()
        xyz = {int(n): coords[3 * i : 3 * i + 3] for i, n in enumerate(node_ids)}
        counts: dict[tuple[int, ...], int] = {}
        for i in range(0, len(faces), 3):
            key = tuple(sorted(int(n) for n in faces[i : i + 3]))
            counts[key] = counts.get(key, 0) + 1
        free = [k for k, c in counts.items() if c == 1]
        on_interface = [k for k in free if all(abs(xyz[n][0] - 10.0) < 1e-6 for n in k)]
    finally:
        gmsh.finalize()
    assert free, "a closed solid mesh has boundary faces"
    assert on_interface == [], f"{len(on_interface)} free faces on the shared face: not one conformal mesh"

    result = NetgenOCC().run(pool, step, timeout_s=TIMEOUT_S)
    assert result.status == "pass", result


def test_overlapping_facets_file_reports_without_raising(pool: WorkerPool) -> None:
    assert OVERLAPPING_FACETS.is_file()
    result = NetgenOCC().run(pool, str(OVERLAPPING_FACETS), timeout_s=TIMEOUT_S)
    assert result.status in ("pass", "fail"), result
    assert result.message, "the outcome must carry a recorded message"
    if result.status == "fail":
        assert result.error_kind


def test_unreadable_step_is_a_fail_not_an_exception(pool: WorkerPool, tmp_path: Path) -> None:
    junk = tmp_path / "junk.step"
    junk.write_text("this is not a STEP file")
    result = NetgenOCC().run(pool, str(junk), timeout_s=TIMEOUT_S)
    assert result.status == "fail", result
    assert result.error_kind == "step_unreadable"
    assert result.message


# --- the quality floor must be able to FAIL a mesh -------------------------------------------------
_GOOD_TET = ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1))


def _write_tet_msh(path: Path, order: tuple[int, int, int, int]) -> str:
    nodes = "".join(f"{i + 1} {x} {y} {z}\n" for i, (x, y, z) in enumerate(_GOOD_TET))
    conn = " ".join(str(i + 1) for i in order)
    path.write_text(
        f"$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n4\n{nodes}$EndNodes\n"
        f"$Elements\n1\n1 4 2 1 1 {conn}\n$EndElements\n"
    )
    return str(path)


def test_floor_passes_a_good_tet_and_fails_a_mirrored_one(tmp_path: Path) -> None:
    """The floor's only evidence of being able to fail: one tet with two vertices swapped (mirrored)
    must read minSICN < 0 and come out `fail`; the same tet, correctly ordered, must pass. Needs no
    Netgen: it is the gmsh measurement and the verdict mapping the member uses."""
    from meshpipeline.cad.repair_v4.verdict.netgen_member import _measure, member_result

    def verdict(order: tuple[int, int, int, int]):
        measured = _measure(_write_tet_msh(tmp_path / f"t{order}.msh", order))
        return measured, member_result("netgen_occ", {"ok": True, "solids": 1, **measured}, 0.0)

    good_m, good = verdict((0, 1, 2, 3))
    bad_m, bad = verdict((0, 1, 3, 2))  # vertices 2 and 3 swapped: the mirror image
    assert good_m["min_sicn"] is not None and good_m["min_sicn"] > 0, good_m
    assert good.status == "pass", good
    assert bad_m["min_sicn"] is not None and bad_m["min_sicn"] < 0, bad_m
    assert bad.status == "fail" and bad.error_kind == "low_quality", bad


def test_the_orientation_conversion_does_not_hide_a_mirrored_tet(tmp_path: Path) -> None:
    """The script swaps vertices 2,3 of EVERY tet (netgen -> gmsh convention). Prove it is a
    bijection, not a mask: a tet inverted in Netgen's convention is still negative after it."""
    from meshpipeline.cad.repair_v4.verdict.netgen_member import _measure

    # Netgen's valid tets are gmsh-inverted; after the uniform swap they are valid. Feed the
    # swapped form of a Netgen-INVERTED tet (= the gmsh-good order, swapped twice-over is identity):
    netgen_valid = (0, 1, 3, 2)  # what Netgen writes for a good tet -> script swaps -> (0, 1, 2, 3)
    netgen_inverted = (0, 1, 2, 3)  # what Netgen writes for a bad tet -> script swaps -> (0, 1, 3, 2)
    def after_swap(order: tuple[int, int, int, int]) -> float:
        a, b, c, d = order
        return _measure(_write_tet_msh(tmp_path / f"s{order}.msh", (a, b, d, c)))["min_sicn"]

    assert after_swap(netgen_valid) > 0
    assert after_swap(netgen_inverted) < 0


def test_netgen_segfault_is_a_vote_against_the_file_not_infrastructure() -> None:
    from meshpipeline.cad.repair_v4.verdict.netgen_member import member_result

    segv = member_result("netgen_occ", {"ok": False, "stage": "crash", "returncode": -11, "error": "died"}, 0.0)
    assert (segv.status, segv.error_kind) == ("crashed", "netgen_crash")
    absent = member_result("netgen_occ", {"ok": False, "stage": "unavailable", "error": "none"}, 0.0)
    assert absent.error_kind == INFRASTRUCTURE_KIND and absent.cause == "toolchain_absent"


# --- D19: Netgen's STEP reader keeps only the FIRST root ------------------------------------------

def _write_roots(shapes: list, path: Path) -> str:
    """One STEP ROOT per shape, in order (not one compound): the layout of lane C12's 0053_00530659."""
    writer = STEPControl_Writer()
    for shape in shapes:
        writer.Transfer(shape, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def _loose_shell(x: float) -> object:
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer

    return TopExp_Explorer(_box(x), TopAbs_SHELL).Current()


def test_a_shell_root_ahead_of_the_solids_no_longer_hides_them(pool: WorkerPool, tmp_path: Path) -> None:
    """Roots [SHELL, SOLID, SOLID]: Netgen's own read sees the shell alone ("STEP file holds no solids"); the member
    now reads every root and meshes the two solids."""
    step = _write_roots([_loose_shell(100.0), _box(0), _box(30)], tmp_path / "shell_first.step")
    ng = netgen_python()
    assert ng is not None
    probe = subprocess.run([ng, "-c", "import sys; from netgen.occ import OCCGeometry; s = OCCGeometry(sys.argv[1]).shape;"
                            " print(len(list(s.solids)))", step], capture_output=True, text=True, timeout=300)
    assert probe.stdout.strip().splitlines()[-1] == "0", probe   # the bug, reproduced: Netgen alone sees no solid
    result = NetgenOCC().run(pool, step, timeout_s=TIMEOUT_S)
    assert result.status == "pass", result
    assert "2 solid(s)" in result.message and "every STEP root" in result.message


def test_a_file_netgen_already_reads_is_meshed_exactly_as_before(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write([_box(0)], tmp_path / "box.step")
    result = NetgenOCC().run(pool, step, timeout_s=TIMEOUT_S)
    assert result.status == "pass" and "every STEP root" not in result.message


def test_a_file_with_no_solid_in_any_root_still_reads_as_no_solids(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_roots([_loose_shell(0.0)], tmp_path / "shell_only.step")
    result = NetgenOCC().run(pool, step, timeout_s=TIMEOUT_S)
    assert result.status == "fail" and result.error_kind == "step_unreadable" and "no solids" in result.message


def test_every_root_of_a_multi_root_file_is_meshed_not_only_the_first(pool: WorkerPool, tmp_path: Path) -> None:
    """D20, owner ruling: roots [SOLID, SOLID] - Netgen alone meshed the first body; the member now meshes both."""
    step = _write_roots([_box(0), _box(30)], tmp_path / "two_roots.step")
    result = NetgenOCC().run(pool, step, timeout_s=TIMEOUT_S)
    assert result.status == "pass", result
    assert "2 solid(s)" in result.message and "every STEP root" in result.message
