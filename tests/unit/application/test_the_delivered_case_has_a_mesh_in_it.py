# A delivered "OpenFOAM case" had no mesh in it, and every check it had to pass, it passed.
#
# MEASURED, job b8f09ae9 (2026-11-27), by streaming the real artifact out of the object store and
# listing it without extracting. openfoam_conjugate_case.tar.gz, 541,795,624 B compressed and
# 2,149,511,501 B uncompressed:
#
#     1,591,774,022  74.05%  constant/{fluid,solid}/polyMesh/boundaryIndex   viewer sidecars
#       403,835,287  18.79%  constant/{fluid,solid}/polyMesh/boundaryQuality viewer sidecars
#       135,416,228   6.30%  mesh.msh                        a decimated boundary surface
#        18,444,681   0.86%  constant/triSurface             the input STLs
#            41,273   0.00%  system/, the jsons, two 400-byte polyMesh/boundary files
#                 0      0%  points, faces, owner, neighbour, cellZones, faceZones, processor*, 0/
#
# The customer's mesh is the 69,785,047,070 B of per-region processor trees. None of it shipped.
# `chtMultiRegionFoam` cannot be run on what was delivered.
#
# WHY NOTHING CAUGHT IT. `members` named `constant` and `system`, which WAS the mesh on a serial
# delivery; on the decomposed path the cells live in `processor*/constant/<region>/polyMesh` and
# `constant/<region>/polyMesh` keeps only the patch table and the sidecars. Both declared members
# existed, so `verify_workspace_contract` passed. `verify_archive` checked member paths, the
# prefix, links and the marker - nothing about content - so it passed too.
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from meshpipeline.application.artifact_uploader import (
    BundleContractError,
    verify_archive,
    verify_workspace_contract,
)
from meshpipeline.engines.registry import get_spec

SPEC = get_spec("cfmesh_conjugate")
REGIONS = ("fluid", "solid")
#: What OpenFOAM reads a mesh out of. `boundary` alone is a patch table, which is what shipped.
CELLS = ("points", "faces", "owner", "neighbour")


def _case(root: Path, *, ranks: int = 0, cells_in_constant: bool = False) -> Path:
    """A conjugate workspace shaped like a real one: the dicts, the report, the plan, the marker,
    and the regions' patch tables and viewer sidecars - with the CELLS either in the per-rank trees
    (a decomposed delivery), in `constant` (a merged one), or nowhere, which is what shipped."""
    (root / "system").mkdir(parents=True, exist_ok=True)
    (root / "system" / "fvSchemes").write_text("x")
    (root / "constant").mkdir(parents=True, exist_ok=True)
    (root / "constant" / "regionProperties").write_text("regions ();")
    for r in REGIONS:
        pm = root / "constant" / r / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        (pm / "boundary").write_text("0()")             # the patch table, as delivered
        (pm / "boundaryIndex").mkdir(exist_ok=True)     # the viewer sidecar, as delivered
        (pm / "boundaryIndex" / "meta.json").write_text("{}")
        if cells_in_constant:
            for f in CELLS:
                (pm / f).write_text("0()")
    for k in range(ranks):
        for r in REGIONS:
            pm = root / f"processor{k}" / "constant" / r / "polyMesh"
            pm.mkdir(parents=True, exist_ok=True)
            for f in CELLS:
                (pm / f).write_text("0()")
    (root / "conjugate_report.json").write_text("{}")
    (root / "conjugate_plan.json").write_text("{}")
    return root


def _bundle(root: Path, out: Path) -> Path:
    """The archive the uploader would build, through the recipe's own expansion."""
    d = SPEC.deliverable
    with tarfile.open(out, "w") as tf:
        for rel in d.expand(root):
            tf.add(root / rel, arcname=f"{d.prefix}/{rel}")
    return out


def test_the_bundle_that_shipped_is_refused(tmp_path):
    """THE DEFECT. Patch tables and sidecars, no cells anywhere - exactly job b8f09ae9's archive.
    Both declared members exist, so the workspace contract is satisfied; the archive must not be."""
    ws = _case(tmp_path / "ws")
    verify_workspace_contract(ws, SPEC)          # as it was: this part really did pass
    with pytest.raises(BundleContractError) as e:
        verify_archive(_bundle(ws, tmp_path / "b.tar"), SPEC)
    said = str(e.value)
    assert "no mesh inside it" in said, said
    for pat in CELLS:
        assert pat in said, (pat, said)


def test_a_decomposed_delivery_carries_its_cells(tmp_path):
    """The real path. The cells are in the per-rank trees and the glob is what reaches them."""
    ws = _case(tmp_path / "ws", ranks=8)
    verify_workspace_contract(ws, SPEC)
    got = SPEC.deliverable.expand(ws)
    assert [p for p in got if p.startswith("processor")] == [f"processor{k}" for k in range(8)]
    verify_archive(_bundle(ws, tmp_path / "b.tar"), SPEC)


def test_a_merged_delivery_carries_its_cells_too(tmp_path):
    """And the other path, which has no processor directories at all: the same content rule is
    satisfied from `constant`, which is why `processor*` is not a required member."""
    ws = _case(tmp_path / "ws", cells_in_constant=True)
    verify_workspace_contract(ws, SPEC)
    assert not [p for p in SPEC.deliverable.expand(ws) if p.startswith("processor")]
    verify_archive(_bundle(ws, tmp_path / "b.tar"), SPEC)


def test_the_rank_count_is_not_written_into_the_recipe(tmp_path):
    """`ranks_for` chooses it per run, so a literal member list would ship seven eighths of a mesh
    on a nine-rank run and nothing on a serial one."""
    for ranks in (1, 2, 9, 16):
        ws = _case(tmp_path / f"ws{ranks}", ranks=ranks)
        got = [p for p in SPEC.deliverable.expand(ws) if p.startswith("processor")]
        assert len(got) == ranks, (ranks, got)
        verify_archive(_bundle(ws, tmp_path / f"b{ranks}.tar"), SPEC)


def test_the_solver_scaffold_travels_when_the_vm_wrote_one(tmp_path):
    """`solver_case.write_solver_case` writes 0/fluid/{T,U,p,p_rgh}, 0/solid/T and a README on the
    VM. It writes thirteen files, counted by running it: SEVEN were already shipping under
    constant/ and system/ (the two thermophysicalProperties, constant/g, and fvSchemes + fvSolution
    for each region) and SIX were left behind - everything under `0/`, which is the five field
    files and the README. So the case arrived without the fields its own README documents."""
    ws = _case(tmp_path / "ws", ranks=4)
    (ws / "0" / "fluid").mkdir(parents=True)
    (ws / "0" / "fluid" / "T").write_text("x")
    (ws / "0" / "README.txt").write_text("x")
    assert "0" in SPEC.deliverable.expand(ws)
    names = tarfile.open(_bundle(ws, tmp_path / "b.tar")).getnames()
    assert f"{SPEC.deliverable.prefix}/0/fluid/T" in names
    assert f"{SPEC.deliverable.prefix}/0/README.txt" in names


def test_an_engine_that_declares_no_content_rule_is_unaffected():
    """Only the engine knows what its own format must carry: gmsh ships an Abaqus deck with no
    `owner` file in it and must not be held to one. An empty rule is no rule."""
    for name in ("gmsh", "snappy", "cfmesh"):
        try:
            other = get_spec(name)
        except Exception:                       # noqa: BLE001 - not every engine is registered
            continue
        if other.deliverable is not None:
            assert other.deliverable.must_contain == (), (name, other.deliverable.must_contain)
