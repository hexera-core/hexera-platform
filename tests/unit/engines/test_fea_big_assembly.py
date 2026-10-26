# Responsibility: Verify the big-assembly speed-ups of the FEA gmsh adapter, each giving the same mesh byte for byte: MESH-6 (each CAD entity consults only the size fields that can size it - a field is skipped on an entity only where its lowest possible value there is above the entity's ceiling), the faces a first try left empty meshed again alone (LAD-fea-6), and MESH-7 (parts meshed apart each filled with only their own surface mesh in the model).
# Boundaries: the bounds are checked against gmsh's own field laws on numbers; the same-mesh checks mesh small built parts in-process (the real case, ox_mc_lid's 253 parts and 2,291 fields, is the lab's: ~/oxlid_lab).
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter as A  # noqa: E402


def _threshold(d, size, base, dmin, dmax):
    """gmsh's Threshold field (linear) at distance d, as gmsh computes it."""
    r = min(max((d - dmin) / (dmax - dmin), 0.0), 1.0)
    return size * (1 - r) + base * r


def _ball(d, radius, thick, v_in, v_out):
    if d < radius:
        return v_in
    if d - radius <= thick:
        return v_in + (d - radius) / thick * (v_out - v_in)
    return v_out


def test_the_lowest_and_highest_a_field_can_be_are_its_law_at_the_nearest_and_farthest_box_points():
    rng = np.random.default_rng(1)
    for _ in range(200):
        lo_a, lo_b = rng.uniform(-1, 1, 3), rng.uniform(-1, 1, 3)
        a = np.concatenate([lo_a, lo_a + rng.uniform(0, 0.5, 3)])
        b = np.concatenate([lo_b, lo_b + rng.uniform(0, 0.5, 3)])
        gap, far = float(A._box_gap(a[None], b)[0]), float(A._box_far(a[None], b)[0])
        # every pair of points, one in each box, lies between the two
        p = a[:3] + rng.uniform(0, 1, (50, 3)) * (a[3:] - a[:3])
        q = b[:3] + rng.uniform(0, 1, (50, 3)) * (b[3:] - b[:3])
        d = np.linalg.norm(p[:, None] - q[None], axis=2)
        assert gap <= d.min() + 1e-12 and d.max() <= far + 1e-12
        size, base, dmin, dmax = 0.001, 0.02, 0.002, 0.07
        for x in d.ravel()[:20]:
            assert float(A._threshold_low(np.array([gap]), size, base, dmin, dmax)[0]) <= _threshold(x, size, base, dmin, dmax)
            assert _threshold(x, size, base, dmin, dmax) <= float(A._threshold_low(np.array([far]), size, base, dmin, dmax)[0])
            assert float(A._ball_low(np.array([gap]), 0.1, 0.3, size, base)[0]) <= _ball(x, 0.1, 0.3, size, base)


def _stage(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    return ws


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _planned(shape, ws: Path, monkeypatch) -> Path:
    """The workspace as the driver leaves it for the mesher's first pass (an assembly staged, its plan)."""
    import shutil

    from meshpipeline.engines import runtime

    _stage(shape, ws / "drive")
    kept = ws / "planned"

    class Stop(Exception):
        pass

    def grab(w, timeout=0):
        shutil.copytree(w, kept)
        raise Stop

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=grab))
    with pytest.raises(Stop):
        asyncio.run(FD.drive(ws / "drive", {"purpose": "structural"}, job_id=f"t-{ws.name}", publish=None,
                             run=_Run(), source_path=str(ws / "drive" / "upload.step")))
    return kept


def _mesh_both(planned: Path, tmp: Path, monkeypatch, capsys) -> tuple[Path, Path, str]:
    import shutil

    on, off = tmp / "on", tmp / "off"
    shutil.copytree(planned, on)
    shutil.copytree(planned, off)
    assert A.mesh(on) == 0
    said = capsys.readouterr().err
    with monkeypatch.context() as m:
        m.setattr(A, "reach_restricted", lambda _g, ids, _b, _base, report=None: ids)
        assert A.mesh(off) == 0
    return on, off, said


def _same(on: Path, off: Path) -> None:
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        assert (on / name).read_bytes() == (off / name).read_bytes(), name


def _restricted(said: str) -> int:
    line = next(ln for ln in said.splitlines() if A.REACH_RULE in ln)
    return int(line.split(f"({A.REACH_RULE}): ")[1].split(" of ")[0])


def test_an_assembly_meshed_consulting_only_the_fields_that_can_size_is_the_same_mesh(tmp_path, monkeypatch, capsys):
    """A plate with a hole beside two small blocks well away from it: the hole's and the walls' fields reach
    the blocks no lower than the blocks' own base, so they are skipped there - and the files are the same,
    byte for byte."""
    shape = fx._compound(fx.plate_with_hole(), fx._box(160, 0, 0, 8, 8, 8), fx._box(160, 40, 0, 6, 6, 3))
    planned = _planned(shape, tmp_path, monkeypatch)
    on, off, said = _mesh_both(planned, tmp_path, monkeypatch, capsys)
    assert _restricted(said) > 0
    _same(on, off)


def test_a_single_part_meshed_consulting_only_the_fields_that_can_size_is_the_same_mesh(tmp_path, monkeypatch, capsys):
    planned = _planned(fx.bracket(), tmp_path, monkeypatch)
    on, off, _said = _mesh_both(planned, tmp_path, monkeypatch, capsys)
    _same(on, off)


def test_a_field_is_kept_wherever_it_can_be_the_smallest(tmp_path):
    """Two thin-wall fields, one at each end of a long bar: on the far end's entities the near field is
    kept and the far one skipped (it can be no lower there than the base), and every entity keeps at least
    the field that can be its smallest."""
    ws = _stage(fx._box(0, 0, 0, 400, 10, 10), tmp_path / "bar")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(ws / "geometry.brep"))
        gmsh.model.occ.synchronize()
        left = [s for _d, s in gmsh.model.getEntities(2) if gmsh.model.getBoundingBox(2, s)[3] < 1e-5]
        right = [s for _d, s in gmsh.model.getEntities(2) if gmsh.model.getBoundingBox(2, s)[0] > 0.4 - 1e-5]
        plan = {"base_size_m": 0.02, "growth": 1.3, "zones": [
            {"kind": "wall", "size_m": 0.001, "reach_m": 0.002, "faces": [f"Face_{left[0]}"]},
            {"kind": "wall", "size_m": 0.0004, "reach_m": 0.001, "faces": [f"Face_{right[0]}"]}]}
        report: dict = {}
        real = A.reach_restricted
        A.reach_restricted = lambda g, ids, b, base, report=report: real(g, ids, b, base, report=report)
        try:
            A.size_fields(gmsh, plan, {t for _d, t in gmsh.model.getEntities(2)})
        finally:
            A.reach_restricted = real
        keep = report["keep"]
        assert len(keep) == 2
        row = {dt: k for k, dt in enumerate(report["dim_tags"])}
        k_left = next(k for k in keep.values() if k[row[(2, left[0])]])
        k_right = next(k for k in keep.values() if k[row[(2, right[0])]])
        assert k_left[row[(2, left[0])]] and not k_left[row[(2, right[0])]]
        assert k_right[row[(2, right[0])]] and not k_right[row[(2, left[0])]]
        assert all(a or b for a, b in zip(k_left, k_right, strict=True))
    finally:
        gmsh.finalize()



def _fingerprint() -> str:
    import hashlib

    h = hashlib.sha256()
    tags, xyz, _p = gmsh.model.mesh.getNodes()
    h.update(np.asarray(tags).tobytes())
    h.update(np.asarray(xyz).tobytes())
    for d in (0, 1, 2):
        for _d, e in gmsh.model.getEntities(d):
            _t, etags, nodes = gmsh.model.mesh.getElements(d, e)
            for a, b in zip(etags, nodes, strict=True):
                h.update(np.asarray(a).tobytes())
                h.update(np.asarray(b).tobytes())
    return h.hexdigest()


def _two_solids() -> None:
    gmsh.model.add("m")
    gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
    gmsh.model.occ.addCylinder(2, 0, 0, 0, 0, 1, 0.4)
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", 0.1)
    gmsh.option.setNumber("Mesh.RandomSeed", 1)


def test_surfaces_meshed_again_alone_give_the_mesh_meshing_everything_again_gives():
    """The surfaces a first try left empty are meshed again with MeshAdapt, alone: the same nodes and elements,
    numbered the same, as clearing the whole model and meshing it all again - and what was visible stays so."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        _two_solids()
        gmsh.model.mesh.generate(2)
        for s in (3, 7):
            gmsh.model.mesh.setAlgorithm(2, s, A.MESH_ADAPT)
        gmsh.model.mesh.clear()
        gmsh.model.mesh.generate(2)
        everything_again = _fingerprint()
        gmsh.model.remove()
        _two_solids()
        hidden = (3, 1)
        gmsh.model.setVisibility([hidden], 0)
        gmsh.model.mesh.generate(2)
        for s in (3, 7):
            gmsh.model.mesh.setAlgorithm(2, s, A.MESH_ADAPT)
        gmsh.model.setVisibility([hidden], 1)
        A.remesh_only(gmsh, [3, 7])
        assert _fingerprint() == everything_again
        assert gmsh.option.getNumber("Mesh.MeshOnlyVisible") == 0
        assert gmsh.model.getVisibility(3, 1) == 1 and gmsh.model.getVisibility(2, 9) == 1
    finally:
        gmsh.finalize()


def test_a_thin_tube_round_other_parts_meshed_consulting_only_the_fields_that_can_size_is_the_same_mesh(
        tmp_path, monkeypatch, capsys):
    """A thin-walled tube with a pin and a block inside its box (ox_mc_lid's bottom connector: a 0.5 mm wall
    40 mm round, its box holding other parts): on the tube's own faces its wall field is near its smallest,
    so the fields of the parts inside are skipped there - and the files are the same, byte for byte."""
    tube = fx._cut(fx._cyl(0, 0, 0, 10, 12), fx._cyl(0, 0, -1, 9, 14))
    shape = fx._compound(tube, fx._cyl(0, 0, 2, 2, 8), fx._box(4, 3, 2, 3, 3, 3))
    planned = _planned(shape, tmp_path, monkeypatch)
    on, off, said = _mesh_both(planned, tmp_path, monkeypatch, capsys)
    assert _restricted(said) > 0
    _same(on, off)


def _fill_one_by_one(vols) -> None:
    """The fill as it was: each volume alone, every other surface's mesh in the model."""
    everything = gmsh.model.getEntities()
    gmsh.option.setNumber("Mesh.MeshOnlyVisible", 1)
    for v in vols:
        gmsh.model.setVisibility(everything, 0)
        gmsh.model.setVisibility([(3, int(v))], 1, recursive=True)
        gmsh.model.mesh.generate(3)
    gmsh.model.setVisibility(everything, 1)
    gmsh.option.setNumber("Mesh.MeshOnlyVisible", 0)


def _parts_apart() -> list[int]:
    gmsh.model.add("apart")
    for i in range(3):
        gmsh.model.occ.addBox(i * 2.0, 0, 0, 1, 1, 1)
        gmsh.model.occ.addCylinder(i * 2.0, 3, 0, 0, 0, 1, 0.4)
    gmsh.model.occ.addBox(0.5, 0.5, 0.5, 1, 1, 1)            # passes into the first box: filled apart only
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", 0.12)
    gmsh.option.setNumber("Mesh.RandomSeed", 1)
    gmsh.model.addPhysicalGroup(3, [t for _d, t in gmsh.model.getEntities(3)], name="solid")
    gmsh.model.mesh.generate(2)
    return [t for _d, t in gmsh.model.getEntities(3)]


def test_each_volume_filled_with_only_its_own_surface_mesh_in_the_model_is_the_same_mesh():
    """MESH-7: the other surfaces' triangles set aside while a volume is filled, put back after: the same nodes,
    elements, tags and orientation as filling each volume with the whole surface mesh in the model."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        _fill_one_by_one(_parts_apart())
        before = _fingerprint_all()
        gmsh.model.remove()
        A.fill_each_volume(gmsh, _parts_apart())
        assert _fingerprint_all() == before
        assert gmsh.option.getNumber("Mesh.MeshOnlyVisible") == 0 and gmsh.option.getNumber("Mesh.Renumber") == 1
    finally:
        gmsh.finalize()


def _fingerprint_all() -> str:
    import hashlib

    h = hashlib.sha256()
    tags, xyz, _p = gmsh.model.mesh.getNodes()
    h.update(np.asarray(tags).tobytes())
    h.update(np.asarray(xyz).tobytes())
    for d in (0, 1, 2, 3):
        for _d, e in gmsh.model.getEntities(d):
            _t, etags, nodes = gmsh.model.mesh.getElements(d, e)
            for a, b in zip(etags, nodes, strict=True):
                h.update(np.asarray(a).tobytes())
                h.update(np.asarray(b).tobytes())
    return h.hexdigest()
