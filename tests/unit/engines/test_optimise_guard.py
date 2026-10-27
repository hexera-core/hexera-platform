# Responsibility: Verify MESH-10: a volume gmsh's optimiser leaves with more invalid elements than it went in with gets its input mesh back, every other volume keeps the optimiser's work, and a run where nothing was made invalid is exactly the optimiser's own.
# Boundaries: small built boxes meshed in-process with real gmsh; the damaging optimiser is a stand-in that turns elements of one volume inside out (no real optimiser run is known to do so yet: MESH-10 is a guard).
from __future__ import annotations

import numpy as np
import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines.gmsh import optimise_guard as G  # noqa: E402


def _two_boxes():
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setNumber("Mesh.RandomSeed", 1)
    gmsh.model.add("two")
    gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
    gmsh.model.occ.addBox(3, 0, 0, 1, 1, 0.2)        # a thin plate, apart from the first
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", 0.25)
    gmsh.model.mesh.generate(3)


def _mesh_of(volume):
    tags, xyz, _ = gmsh.model.mesh.getNodes(3, volume, includeBoundary=True)
    order = np.argsort(tags)
    types, etags, enodes = gmsh.model.mesh.getElements(3, volume)
    tets = np.asarray(enodes[0], dtype=np.int64).reshape(-1, 4)
    # each element as its corners' places, independent of tags
    place = dict(zip(np.asarray(tags)[order].tolist(), np.asarray(xyz).reshape(-1, 3)[order].tolist(), strict=True))
    shapes = sorted(tuple(sorted(tuple(round(c, 12) for c in place[int(n)]) for n in t)) for t in tets)
    return len(etags[0]), shapes


def _invalid(volume):
    return G._invalid(gmsh, volume)


def _whole_model():
    tags, xyz, _ = gmsh.model.mesh.getNodes()
    types, etags, enodes = gmsh.model.mesh.getElements()
    return (np.asarray(tags).tolist(), np.round(np.asarray(xyz), 14).tolist(),
            [np.asarray(t).tolist() for t in etags], [np.asarray(n).tolist() for n in enodes])


def test_a_volume_the_optimiser_turns_inside_out_gets_its_input_mesh_back_and_the_other_keeps_its_work(monkeypatch):
    _two_boxes()
    try:
        gmsh.model.mesh.optimize("Netgen")
        netgen_1 = _mesh_of(1)
    finally:
        gmsh.finalize()
    _two_boxes()
    try:
        assert _invalid(1) == 0 and _invalid(2) == 0
        before_2 = _mesh_of(2)
        real = gmsh.model.mesh.optimize

        def damaging(method="", *a, **k):
            real(method, *a, **k)
            # the stand-in's harm: every inside node of the plate pushed far through its top face
            tags, xyz, _ = gmsh.model.mesh.getNodes(3, 2, includeBoundary=False)
            if len(tags) == 0:   # a plate with no inside node: move a face node instead
                raise AssertionError("the test plate needs inside nodes")
            for t, p in zip(tags, np.asarray(xyz).reshape(-1, 3), strict=True):
                gmsh.model.mesh.setNode(int(t), [p[0], p[1], p[2] + 5.0], [])

        monkeypatch.setattr(gmsh.model.mesh, "optimize", damaging)
        record = G.optimise(gmsh, "Netgen", 1)
        assert set(record["volumes"]) == {"2"}
        assert record["rule"] == "MESH-10" and record["optimiser"] == "Netgen"
        assert record["volumes"]["2"]["invalid_before"] == 0
        assert record["volumes"]["2"]["invalid_after"] > 0
        assert record["volumes"]["2"]["invalid_now"] == 0
        # the plate is exactly the mesh it went in with; every element valid
        assert _mesh_of(2) == before_2
        assert _invalid(2) == 0 and _invalid(1) == 0
        # the cube keeps what the optimiser made of it (here: the real Netgen's work)
        assert _mesh_of(1) == netgen_1
        # numbered from 1, no gaps, as after a fill
        tags, _, _ = gmsh.model.mesh.getNodes()
        assert sorted(np.asarray(tags).tolist()) == list(range(1, len(tags) + 1))
        etags = np.concatenate([np.asarray(t) for t in gmsh.model.mesh.getElements()[1]])
        assert sorted(etags.tolist()) == list(range(1, len(etags) + 1))
    finally:
        gmsh.finalize()


def test_when_the_optimiser_makes_nothing_invalid_the_model_is_exactly_what_it_left():
    _two_boxes()
    try:
        assert G.optimise(gmsh, "Netgen", 1) == {}
        guarded = _whole_model()
    finally:
        gmsh.finalize()
    _two_boxes()
    try:
        gmsh.model.mesh.optimize("Netgen")
        plain = _whole_model()
    finally:
        gmsh.finalize()
    assert guarded == plain


def test_no_pass_asked_for_runs_no_optimiser(monkeypatch):
    _two_boxes()
    try:
        before = _whole_model()
        monkeypatch.setattr(gmsh.model.mesh, "optimize", lambda *a, **k: pytest.fail("optimiser ran"))
        assert G.optimise(gmsh, "Netgen", 0) == {}
        assert _whole_model() == before
    finally:
        gmsh.finalize()
