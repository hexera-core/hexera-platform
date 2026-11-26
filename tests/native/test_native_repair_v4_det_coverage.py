# Responsibility: On the REAL kernel, the deterministic sweep reaches the four ops that used to be AI-only, from a site
#                 whose facts the kernel itself measured, and the candidate it proposes is one the op RUNS (not one it
#                 declines or the schema refuses). Fixtures are the ops' own native fixtures, so "the op works on this"
#                 is already proved there; what is proved here is that DetGenerator's targets and parameters get there.
# Boundaries: the native tier (OCP in spawned workers). The search driver is not run: `DetGenerator.propose` on one
#             state, then `REGISTRY[op].apply` on the real pool. Unit coverage: tests/unit/cad/repair_v4/test_det_coverage.py.
from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace  # noqa: E402
from tests.native import test_native_repair_v4_ops_edges as E  # noqa: E402
from tests.native import test_native_repair_v4_ops_faces as F  # noqa: E402

from meshpipeline.cad.repair_v4 import sites as sites_mod  # noqa: E402
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined  # noqa: E402
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState  # noqa: E402
from meshpipeline.cad.repair_v4.types import Site  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 300


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _site(pool: WorkerPool, ws: Workspace, kind: str, entities: list[str]) -> Site:
    """A site of `kind` on `entities`, its k=2 neighbourhood MEASURED by the kernel (`sites._attach_graphs`)."""
    site = Site(id="S1", kind=kind, entities=entities, xyz=[], meshers_failing=["gmsh_default"], dist_over_h=None,
                graph={}, view_png=None)  # type: ignore[arg-type]
    sites_mod._attach_graphs(pool, ws, [site], sites_mod._reverse(ws), 1.0)
    assert "error" not in site.graph, site.graph
    return site


def _proposed(pool: WorkerPool, ws: Workspace, site: Site, op: str) -> list[Any]:
    gen = DetGenerator()
    gen.bind_pool(pool)  # the real ws_diagonal, as search() binds it
    state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=[site], depth=0,
                        parent=None, via=None)
    got = [c for c in gen.propose(state, 50) if c.op == op]
    assert got, f"{op} was not proposed: {gen.notes}"
    return got


def _runs(pool: WorkerPool, ws: Workspace, cands: list[Any]) -> list[tuple[Any, Any, Any]]:
    """Apply each candidate on the real pool; (candidate, new workspace, diff) for the ones that applied."""
    applied = []
    for c in cands:
        try:
            new, diff = REGISTRY[c.op].apply(pool, ws, list(c.targets), dict(c.params), timeout_s=TIMEOUT)
        except OpDeclined:
            continue
        if diff.params.get("declined") is None:
            applied.append((c, new, diff))
    return applied


def test_det_rebuilds_the_inflated_edge_the_kernel_measured(pool: WorkerPool, tmp_path: Path) -> None:
    path, eindex = E._inflated_box(tmp_path)
    ws = Workspace.from_brep(pool, path)
    edge_id = E._id_of(ws, "E", eindex)
    site = _site(pool, ws, "proximity", [edge_id])
    cands = _proposed(pool, ws, site, "rebuild_edge_as_intersection")
    assert [c.targets for c in cands] == [[edge_id]], "only the inflated edge exceeds 1e-5 x diagonal"
    [(_, new, diff)] = _runs(pool, ws, cands)
    assert edge_id in diff.modified and E._valid(E._read(new.brep_path))


def test_det_rebuilds_a_self_intersecting_face(pool: WorkerPool, tmp_path: Path) -> None:
    surface = F._bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0)
    part = F._with_distant_neighbour(F._looped_face(surface, 40.0, loop=0.05))
    ws = Workspace.from_brep(pool, F._write(part, tmp_path / "looped.brep"))
    (face_id,) = [s for s, i in F._ids_at(ws, "F").items()
                  if F._surface_kind_of(F._faces(part)[i - 1]) == "BSplineSurface"]
    cands = _proposed(pool, ws, _site(pool, ws, "self_overlap", [face_id]), "rebuild_face")
    assert [(c.targets, c.params) for c in cands] == [([face_id], {"continuity": "G0"})]
    [(_, new, _diff)] = _runs(pool, ws, cands)
    (rebuilt,) = [f for f in F._faces(F._read(new.brep_path)) if F._surface_kind_of(f) == "BSplineSurface"]
    assert not F._self_intersects(rebuilt)


def test_det_reapproximates_a_high_degree_free_form_face(pool: WorkerPool, tmp_path: Path) -> None:
    surface = F._bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0, n=14, deg=(8, 8))
    face = BRepBuilderAPI_MakeFace(surface, 1e-7).Face()
    ws = Workspace.from_brep(pool, F._write(face, tmp_path / "deg8.brep"))
    (face_id,) = [s for s in ws.ids if s.startswith("F")]
    site = _site(pool, ws, "periodic", [face_id])
    assert {n["surface"] for n in site.graph["nodes"]} == {"bsplinesurface"}, "the kernel names the surface type"
    cands = _proposed(pool, ws, site, "reapproximate")
    bound = F._bound(face)
    assert [c.params["max_degree"] for c in cands] == [9, 6]
    assert [c.params["tol_mm"] for c in cands] == pytest.approx([0.25 * bound, 0.5 * bound], rel=1e-6)
    applied = _runs(pool, ws, cands)
    assert applied, "no rung of the sweep was accepted by the kernel"
    for c, new, _ in applied:
        (out,) = F._faces(F._read(new.brep_path))
        s = BRep_Tool.Surface_s(out)
        assert max(s.UDegree(), s.VDegree()) <= c.params["max_degree"]
        assert F._dist(F._surface_points(face), out) <= c.params["tol_mm"] + 1e-9


def test_det_refits_a_split_patch_as_one_face(pool: WorkerPool, tmp_path: Path) -> None:
    path, patch = F._split_patch_shell(tmp_path)
    ws = Workspace.from_brep(pool, path)
    cluster = sorted(s for s in ws.ids if s.startswith("F"))
    cands = _proposed(pool, ws, _site(pool, ws, "sliver", cluster), "refit_cluster")
    bound = F._bound(patch)
    assert [c.params["tol_mm"] for c in cands] == pytest.approx([0.5 * bound, bound], rel=1e-6)
    assert all(sorted(c.targets) == cluster for c in cands)
    c, new, diff = _runs(pool, ws, cands)[0]
    assert c is cands[0], "the mildest rung already applies on this fixture"
    assert diff.declared_removed == cluster and len(diff.declared_added) == 1
