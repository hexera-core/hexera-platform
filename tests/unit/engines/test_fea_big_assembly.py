# Responsibility: Verify the big-assembly speed-ups of the FEA gmsh adapter, each giving the same mesh byte for byte: MESH-6 (each CAD entity consults only the size fields that can size it - a field is skipped on an entity only where its lowest possible value there is above the entity's ceiling), the faces a first try left empty meshed again alone (LAD-fea-6), and MESH-7 (parts meshed apart each filled with only their own surface mesh in the model).
# Boundaries: the bounds are checked against gmsh's own field laws on numbers; the same-mesh checks mesh small built parts in-process (the real case, ox_mc_lid's 253 parts and 2,291 fields, is the lab's: ~/oxlid_lab).
from __future__ import annotations

import asyncio
import json
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


def _planned(shape, ws: Path, monkeypatch, state: dict | None = None) -> Path:
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
        asyncio.run(FD.drive(ws / "drive", state or {"purpose": "structural"}, job_id=f"t-{ws.name}", publish=None,
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
    tube = fx._cut(fx._cyl(0, 0, 0, 8, 6), fx._cyl(0, 0, -1, 6, 8))
    shape = fx._compound(tube, fx._cyl(0, 0, 1, 2, 4), fx._box(3, 2, 1, 2, 2, 2))
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


def _fail_volume(monkeypatch, volume: int) -> None:
    """gmsh's fill raises whenever `volume` is the one being filled (as Part_239's did on ox_mc_lid)."""
    real = gmsh.model.mesh.generate

    def generate(dim=3):
        if dim == 3 and gmsh.option.getNumber("Mesh.MeshOnlyVisible") and gmsh.model.getVisibility(3, volume):
            raise Exception(f"Invalid boundary mesh (overlapping facets) on surface {volume * 100} surface {volume * 100 + 1}")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate))


def test_a_part_the_fill_fails_on_is_meshed_again_alone_and_the_others_are_kept(tmp_path, monkeypatch, capsys):
    """LAD-fea-11: parts meshed apart (contact) - the fill fails on one; every other part is filled all the same
    and kept, and the next pass meshes only the part that failed - a valid mesh of every part."""
    import shutil

    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 8, 8, 8), fx._box(40, 0, 0, 6, 6, 6))
    planned = _planned(shape, tmp_path, monkeypatch, {"purpose": "structural", "fea_connection": "contact"})
    whole = tmp_path / "whole"
    shutil.copytree(planned, whole)
    assert A.mesh(whole) == 0
    first = tmp_path / "first"
    shutil.copytree(planned, first)
    with monkeypatch.context() as m:
        _fail_volume(m, 2)
        with pytest.raises(Exception, match="overlapping facets"):
            A.mesh(first)
    kept = json.loads((first / A.KEEP_JSON).read_text())
    # the fill stops at the part that failed (gmsh refuses every mesh call after an error): Part_3 was not reached
    assert kept["failed_volumes"] == [2] and kept["kept_volumes"] == [1] and kept["rule"] == A.KEEP_RULE
    assert kept["failed_parts"] == ["Part_2"] and (first / A.KEEP_MSH).exists()
    capsys.readouterr()
    plan = json.loads((first / "fea_plan.json").read_text())
    plan["mesh"] = {**(plan.get("mesh") or {}), "keep": {"rule": A.KEEP_RULE, "from_pass": 1}}
    (first / "fea_plan.json").write_text(json.dumps(plan))
    assert A.mesh(first) == 0
    said = capsys.readouterr().err
    assert "1 part volume(s) kept from the pass before; meshing the other 2" in said
    q, q_whole = (json.loads((w / "quality.json").read_text()) for w in (first, whole))
    # the failed pass went on to the CAD's own scale (MESH-4), where it kept its parts: so does this one
    assert q["fea_mesh_scale"] == "mm" and not q["fatal"] and q["min_sicn"] > 0.0
    assert 0.9 * q_whole["cells"] <= q["cells"] <= 1.1 * q_whole["cells"]
    deck = (first / "mesh.inp").read_text()
    for p in (1, 2, 3):
        assert f"Part_{p}_Face_1" in deck


def test_a_pass_with_nothing_kept_reads_no_kept_file(tmp_path):
    """LAD-fea-11 needs the plan to say so and the files to be there; anything else is an ordinary pass."""
    assert A.kept_for(tmp_path, {}) is None
    assert A.kept_for(tmp_path, {"keep": {"from_pass": 1}}) is None
    (tmp_path / A.KEEP_JSON).write_text(json.dumps({"kept_volumes": [1], "scale": 1000.0,
                                                    "tags": {"surface_tag": {"0": 3}, "volume_tag": {"0": 1}}}))
    assert A.kept_for(tmp_path, {"keep": {"from_pass": 1}}) is None          # no mesh file beside it
    (tmp_path / A.KEEP_MSH).write_text("")
    got = A.kept_for(tmp_path, {"keep": {"from_pass": 1}})
    assert got["scale"] == 1000.0 and got["tags"] == {"surface_tag": {0: 3}, "volume_tag": {0: 1}}


def test_the_driver_meshes_only_the_failed_part_on_its_next_pass(tmp_path, monkeypatch, capsys):
    """LAD-fea-11 through the real driver: pass 1 fills every part but one; pass 2's plan keeps the rest and
    the mesher meshes only that one - and that pass is the one delivered."""
    from meshpipeline.engines import runtime

    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 8, 8, 8), fx._box(40, 0, 0, 6, 6, 6))
    ws = _stage(shape, tmp_path / "drive")
    calls: list[int] = []

    def run_mesher(w, timeout=0):
        calls.append(1)
        with monkeypatch.context() as m:
            if len(calls) == 1:
                _fail_volume(m, 2)
            try:
                return {"rc": A.mesh(w)}
            except Exception:  # noqa: BLE001 - as the runner sees a pass that raised
                return {"rc": 1}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=run_mesher))
    ok, marker, _o = asyncio.run(FD.drive(ws, {"purpose": "structural", "fea_connection": "contact"},
                                          job_id="t-keep", publish=None, run=_Run(),
                                          source_path=str(ws / "upload.step")))
    assert ok, marker
    said = capsys.readouterr().err
    assert "1 part volume(s) kept from the pass before; meshing the other 2" in said
    passes = json.loads((ws / "fea_passes.json").read_text())
    assert [p["valid"] for p in passes["passes"]] == [False, True] and passes["delivered"] == 2
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert plan["mesh"]["keep"] == {"rule": A.KEEP_RULE, "from_pass": 1}


def test_a_part_gmsh_leaves_empty_without_an_error_is_never_kept(tmp_path, monkeypatch):
    """LAD-fea-11: gmsh can leave a volume with no elements and only warn. That part is a failed one: it is
    meshed again on the next pass, never kept (kept, it would reach the solver files with no elements)."""
    import shutil

    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 8, 8, 8), fx._box(40, 0, 0, 6, 6, 6))
    planned = _planned(shape, tmp_path, monkeypatch, {"purpose": "structural", "fea_connection": "contact"})
    ws = tmp_path / "hollow"
    shutil.copytree(planned, ws)
    real = gmsh.model.mesh.generate

    def generate(dim=3):
        if dim == 3 and gmsh.option.getNumber("Mesh.MeshOnlyVisible"):
            if gmsh.model.getVisibility(3, 2):
                return None                                    # left empty, no error
            if gmsh.model.getVisibility(3, 3):
                raise Exception("Invalid boundary mesh (overlapping facets) on surface 13 surface 14")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate))
    with pytest.raises(Exception):
        A.mesh(ws)
    kept = json.loads((ws / A.KEEP_JSON).read_text())
    assert kept["kept_volumes"] == [1] and kept["failed_volumes"] == [2, 3]
    assert kept["failed_parts"] == ["Part_2", "Part_3"]
    # and when no fill raises but one leaves its volume empty, the others are kept all the same
    ws2 = tmp_path / "hollow_only"
    shutil.copytree(planned, ws2)

    def generate2(dim=3):
        if dim == 3 and gmsh.option.getNumber("Mesh.MeshOnlyVisible") and gmsh.model.getVisibility(3, 2):
            return None
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate2))
    with pytest.raises(Exception, match="no elements"):
        A.mesh(ws2)
    kept = json.loads((ws2 / A.KEEP_JSON).read_text())
    assert kept["kept_volumes"] == [1, 3] and kept["failed_volumes"] == [2]


def test_after_a_real_gmsh_error_the_fill_stops_and_keeps_only_what_it_filled():
    """gmsh refuses every mesh call after an error in the same process ("I'm busy! Ask me that later", ox_mc_lid,
    27 Oct: the 14 parts after Part_239 came back empty). The fill stops at the part that failed and says which it
    reached; the parts after it are neither kept nor failed - the next pass fills them, the failed part last."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("lock")
        for i in range(3):
            gmsh.model.occ.addBox(i * 2.0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.3)
        gmsh.model.mesh.generate(2)
        # a facet laid twice on volume 2's first face: gmsh's own "overlapping facets" error in its fill
        face = abs(gmsh.model.getBoundary([(3, 2)], oriented=False)[0][1])
        _t, _tags, nodes = gmsh.model.mesh.getElements(2, face)
        a, b, c = (int(n) for n in nodes[0][:3])
        gmsh.model.mesh.addElementsByType(face, 2, [int(gmsh.model.mesh.getMaxElementTag()) + 1], [a, c, b])
        with pytest.raises(A.PartsFailed) as got:
            A.fill_each_volume(gmsh, [1, 2, 3])
        assert got.value.volumes == [2] and got.value.attempted == [1, 2]
        assert A.empty_volumes(gmsh, [1, 2, 3]) == [2, 3]
    finally:
        gmsh.finalize()


def test_a_kept_part_with_tiny_faces_keeps_its_merges_on_the_pass_that_reads_it_back(tmp_path, monkeypatch, capsys):
    """LAD-fea-11 x MESH-9: a kept part whose tiny faces pass 1 merged keeps them merged on the next pass - its
    faces frozen out of that pass's merges, its merges read back with its mesh, folded in and recorded as a pass
    that meshed it whole would (the same faces merged, the same named faces in the deck)."""
    import re
    import shutil

    shape = fx._compound(fx.strip_block(), fx._box(40, 0, 0, 8, 8, 8))
    planned = _planned(shape, tmp_path, monkeypatch, {"purpose": "structural", "fea_connection": "contact"})
    whole = tmp_path / "whole"
    shutil.copytree(planned, whole)
    assert A.mesh(whole) == 0
    first = tmp_path / "first"
    shutil.copytree(planned, first)
    with monkeypatch.context() as m:
        _fail_volume(m, 2)
        with pytest.raises(Exception, match="overlapping facets"):
            A.mesh(first)
    kept = json.loads((first / A.KEEP_JSON).read_text())
    assert kept["kept_parts"] == ["Part_1"] and kept["failed_parts"] == ["Part_2"]
    assert kept["tiny"]["faces"], "the kept part's merges travel with its mesh"
    capsys.readouterr()
    plan = json.loads((first / "fea_plan.json").read_text())
    plan["mesh"] = {**(plan.get("mesh") or {}), "keep": {"rule": A.KEEP_RULE, "from_pass": 1}}
    (first / "fea_plan.json").write_text(json.dumps(plan))
    assert A.mesh(first) == 0
    assert "1 part volume(s) kept from the pass before" in capsys.readouterr().err
    q, q_whole = (json.loads((w / "quality.json").read_text()) for w in (first, whole))
    assert not q["fatal"] and q["min_sicn"] > 0.0

    def merged(rec):
        return sorted((tuple(r["face"]), tuple(r["into"])) for r in (rec.get("fea_tiny") or {}).get("merged") or [])

    assert merged(q) and merged(q) == merged(q_whole)

    def faces(ws):
        return sorted(set(re.findall(r"(Part_1_Face_\d+)\n", (ws / "mesh.inp").read_text())))

    assert faces(first) == faces(whole)
