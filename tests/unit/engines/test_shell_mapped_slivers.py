# Responsibility: Hold SHMESH-38: SHMESH-12's sliver merge reads a mapped strip's edges (SHMESH-16) as slivers only under SHORT_SHARE of the floor, so a rounded corner meshed as a mapped strip keeps its nodes on the mid-surface.
# Boundaries: gmsh and OpenCASCADE in this process, on one made-up square hollow section; no solver.
from __future__ import annotations

import numpy as np

W, T, R_OUT = 40.0, 2.0, 4.0           # SHS 40 x 40 x 2, corners rounded 4 outside and 2 inside: concentric


def _off_mid(xyz: np.ndarray) -> np.ndarray:
    """Each node's distance from the section's mid-surface: a rounded square 38 wide whose corners are quarter
    circles of radius R_OUT - T/2 about centres (W/2 - R_OUT) off the axis, the tube along z."""
    c = 0.5 * (xyz.min(axis=0) + xyz.max(axis=0))
    ax, ay = np.abs(xyz[:, 0] - c[0]), np.abs(xyz[:, 1] - c[1])
    h, r, k = W / 2 - T / 2, R_OUT - T / 2, W / 2 - R_OUT
    qx, qy = ax - k, ay - k
    corner = (qx > 0) & (qy > 0)
    flat = np.abs(np.maximum(ax, ay) - h)
    return np.where(corner, np.abs(np.hypot(qx, qy) - r), flat)


def _mesh(tmp_path, monkeypatch, share: bool):
    from OCP.BRepTools import BRepTools
    from tests import shell_fixtures as sf

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.gmsh import shell_adapter as sa

    monkeypatch.setattr(sa, "MAPPED_SLIVER_SHARE", share)
    rec, surface = ms.extract(sf.rounded_tube(w=W, t=T, r=R_OUT - T, outer_r=R_OUT, length=200.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    brep = tmp_path / f"shs_{share}.brep"
    BRepTools.Write_s(surface, str(brep))
    return sa.mesh_shell(brep, rec)


def test_a_rounded_corner_keeps_its_nodes_on_the_mid_surface(tmp_path, monkeypatch):
    mesh, applied = _mesh(tmp_path, monkeypatch, True)
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"], q.get("nastran_past")
    used = np.unique(np.concatenate([mesh.quads.ravel(), mesh.trias.ravel()]))
    off = _off_mid(mesh.xyz[used])
    assert off.max() <= 0.3 * T, off.max()


def test_without_it_the_corner_edges_are_merged_into_the_hollow(tmp_path, monkeypatch):
    # the measured fault SHMESH-38 removes: with the plain sliver test the walls' nodes stand off the mid-surface
    mesh, _applied = _mesh(tmp_path, monkeypatch, False)
    used = np.unique(np.concatenate([mesh.quads.ravel(), mesh.trias.ravel()]))
    assert _off_mid(mesh.xyz[used]).max() > 0.3 * T
