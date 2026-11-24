# Responsibility: A gmsh failure that names no surface in its exception is still LOCATED from what gmsh printed: TetGen's
#                 "duplicated facets" node triples and HXT's "el1 = np = 3 ..." triangles become surfaces (box
#                 fingerprints) and points; a low-quality mesh is located at its worst tets' boundary nodes. Lane C5:
#                 8 of 13 conclusive smoke failures reached the search as `unlocated` sites, so it had nothing to aim at.
# Boundaries: pure helpers with a fake gmsh node table; the real gmsh path is the native tier.
from __future__ import annotations

import types

from meshpipeline.cad.repair_v4.verdict import mesh_child

#: What gmsh 4.15.2 printed on the smoke's 0550171 file (default profile: PLC; prod profile: HXT), verbatim.
PLC_OUTPUT = """Found two duplicated facets.
  1st: [203, 271, 282] #19
  2nd: [203, 271, 282] #19
"""
HXT_OUTPUT = """el1 = np = 3 204 324 326
el2 = np = 3 46 341 223
layer1 = 1
layer2 = 1
el1 = np = 3 204 324 326
el2 = np = 3 223 340 46
"""


def test_tetgen_and_hxt_node_triples_are_read_once_each_in_order() -> None:
    assert mesh_child.triangles_in(PLC_OUTPUT) == [(203, 271, 282)]
    assert mesh_child.triangles_in(HXT_OUTPUT) == [(204, 324, 326), (46, 341, 223), (223, 340, 46)]
    assert mesh_child.triangles_in("Info: nothing to see\n") == []


def test_the_triangle_list_is_capped() -> None:
    text = "".join(f"el1 = np = 3 {i} {i + 1} {i + 2}\n" for i in range(1, 300, 3))
    assert len(mesh_child.triangles_in(text)) == mesh_child.MAX_OUTPUT_TRIANGLES


class _Nodes:
    """`gmsh.model.mesh.getNode(tag) -> (coord, parametric, dim, entity tag)` over a small table."""

    def __init__(self, table: dict[int, tuple[tuple[float, float, float], int, int]]) -> None:
        self.table = table

    def getNode(self, tag: int):  # noqa: N802 - gmsh's name
        if tag not in self.table:
            raise Exception(f"unknown node {tag}")  # gmsh raises a plain Exception
        xyz, dim, ent = self.table[tag]
        return list(xyz), [], dim, ent


def _gmsh(table: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(model=types.SimpleNamespace(mesh=_Nodes(table)))


BOXES = {19: (0.0, 0.0, 0.0, 10.0, 10.0, 0.0), 7: (0.0, 0.0, 0.0, 0.0, 10.0, 10.0)}


def test_nodes_name_their_surfaces_as_fingerprints_and_their_triangle_as_a_point() -> None:
    g = _gmsh({203: ((0, 0, 0), 2, 19), 271: ((3, 0, 0), 2, 19), 282: ((0, 3, 0), 1, 4)})
    located = mesh_child.located_from_nodes(g, [(203, 271, 282)], BOXES, 1.0, 0.01)
    surfaces = [x for x in located if x["entity"]]
    points = [x for x in located if x["xyz"] is not None]
    assert [x["mesher_tag"] for x in surfaces] == ["surface 19"]
    assert surfaces[0]["entity"] == mesh_child._located(  # the same fingerprint the surface path gives
        types.SimpleNamespace(kind="x", surfaces=(19,), surfaces_from_log=False, stage="2d", point=None),
        BOXES, 1.0, 0.01)[0]["entity"]
    assert points == [{"entity": None, "xyz": [1.0, 1.0, 0.0], "mesher_tag": "nodes 203,271,282"}]


def test_points_are_in_millimetres_and_unknown_nodes_are_skipped() -> None:
    g = _gmsh({1: ((0.001, 0.0, 0.0), 2, 7), 2: ((0.0, 0.002, 0.0), 2, 7)})  # the prod profile reads metres
    located = mesh_child.located_from_nodes(g, [(1, 2, 999)], BOXES, 1e-3, 0.01)
    (point,) = [x for x in located if x["xyz"] is not None]
    assert point["xyz"] == [0.5, 1.0, 0.0]


def test_a_surface_with_no_box_gives_no_fingerprint_but_the_point_survives() -> None:
    g = _gmsh({5: ((1, 1, 1), 2, 42)})
    located = mesh_child.located_from_nodes(g, [(5, 5, 5)], BOXES, 1.0, 0.01)
    assert [x for x in located if x["entity"]] == [] and len(located) == 1


def test_worst_tets_are_the_lowest_qualities_first() -> None:
    ids, quals = [10, 11, 12, 13], [0.5, 0.001, 0.2, 0.004]
    assert mesh_child.worst(ids, quals, 2) == [11, 13]


def test_the_capture_sees_c_level_output_on_both_streams_and_gives_it_back() -> None:
    """TetGen and HXT print with printf, past Python's sys.stdout; one of them writes to stderr. The capture must see
    both at the file-descriptor level, then put them back so nothing the caller used to read is lost."""
    import subprocess
    import sys

    code = (
        "import ctypes, os\n"
        "from meshpipeline.cad.repair_v4.verdict import mesh_child\n"
        "libc = ctypes.CDLL(None)\n"
        "with mesh_child._captured_stdout() as got:\n"
        "    libc.printf(b'el1 = np = 3 204 324 326\\n')\n"
        "    os.write(2, b'  1st: [203, 271, 282] #19\\n')\n"
        "print('CAPTURED', mesh_child.triangles_in(got['text']))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "CAPTURED [(204, 324, 326), (203, 271, 282)]" in out.stdout
    assert "el1 = np = 3 204 324 326" in out.stdout and "[203, 271, 282]" in out.stderr  # given back


# ------------------------------------------------------------------ D17: gmsh's box padding, in model units

def test_a_millimetre_box_is_unchanged_so_the_mm_members_locate_exactly_as_before() -> None:
    box = (1.7945, -5.0800001, -1e-07, 6.8745, 1e-07, 20.3200001)
    assert mesh_child.box_mm(box, 1.0) == list(box)                # bit for bit


def test_a_metre_box_loses_gmshs_padding_and_gets_the_kernels_back() -> None:
    """gmsh_prod imports in metres: gmsh's 1e-7 pad is 1e-4 mm there. The result is the kernel's box: the true extent
    plus 1e-7 mm, so the fingerprint lands in the kernel's cell."""
    true = (10.0, 20.0, 0.0, 30.0, 40.0, 5.0)
    gmsh_box = tuple(v / 1000.0 - 1e-7 for v in true[:3]) + tuple(v / 1000.0 + 1e-7 for v in true[3:])
    got = mesh_child.box_mm(gmsh_box, 1e-3)
    want = [v - 1e-7 for v in true[:3]] + [v + 1e-7 for v in true[3:]]
    assert all(abs(a - b) < 1e-9 for a, b in zip(got, want, strict=True)), (got, want)
    from meshpipeline.cad.repair.identity import fingerprint_of, quantum_for
    q = quantum_for(104.0)
    kernel = fingerprint_of("face", {"bbox_min": want[:3], "bbox_max": want[3:]}, quantum=q)
    assert fingerprint_of("face", {"bbox_min": got[:3], "bbox_max": got[3:]}, quantum=q) == kernel


def test_a_planar_face_thinner_than_the_padding_keeps_its_plane() -> None:
    got = mesh_child.box_mm((0.0, 0.0, 0.005 - 1e-7, 0.01, 0.01, 0.005 + 1e-7), 1e-3)
    assert abs(got[2] - 5.0) < 1e-6 and abs(got[5] - 5.0) < 1e-6 and got[2] <= got[5]


def test_located_surfaces_carry_their_kernel_box_for_the_workspace_to_name() -> None:
    failure = types.SimpleNamespace(kind="other", surfaces=(3,), surfaces_from_log=False, stage="3d", point=None)
    pad = 1e-7
    box = (0.010 - pad, 0.020 - pad, 0.0 - pad, 0.030 + pad, 0.040 + pad, 0.005 + pad)
    [loc] = mesh_child._located(failure, {3: box}, 1e-3, 1.28e-4)
    assert loc["box"] == mesh_child.box_mm(box, 1e-3) and loc["entity"]
