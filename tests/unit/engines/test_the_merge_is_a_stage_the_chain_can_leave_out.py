# Responsibility: the chain may deliver its regions decomposed, and must default to merging them.
# Boundaries: the chain's shape and the setting that picks it. No OpenFOAM, no subprocess.
"""`reconstructParMesh` is 4,857 s that produces nothing a solver needs.

Measured on the first customer cold plate: 1,351 s for the fluid and chip and 3,506 s for the
solid, 41% of a 3.00 h run, on ONE core of sixty-four. An OpenFOAM solver runs `processor*/`
directly, and a customer handed a merged mesh decomposes it again before solving - so the merge
writes the whole mesh out in order to be undone.

It cannot simply be deleted. Five readers at home wanted `constant/<region>/polyMesh`, which is
why the chain had to learn to describe its regions from the per-rank meshes first
(`_publish_decomposed_boundaries`, `gates.meshed_regions`), and why the port cut had to move
ahead of it (`_port_chain`). With those in place the merge is the last thing standing between a
decomposed mesh and a delivery, and this makes it optional.

TWO THINGS THIS FILE REFUSES TO LET SLIP.

The default is to merge. Every delivered mesh so far was merged, and a setting that changed what
an already-measured part builds would make every past measurement unreadable.

A serial mesh always merges, whatever the setting says. A part that fits one process has no
per-rank copies, so there is nothing to not-merge, and the whole corpus keeps the exact chain it
was measured with.
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from meshpipeline.engines import determinism as det
from meshpipeline.engines.cfmesh_conjugate.conjugate import build
from meshpipeline.engines.cfmesh_conjugate.plan import ConjugatePlan

# -- the setting --------------------------------------------------------------------------

def test_a_deployment_that_says_nothing_merges():
    assert det.conjugate_merge_regions() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " 0 "])
def test_the_recognised_ways_of_saying_no(raw, monkeypatch):
    monkeypatch.setattr(det, "CONJUGATE_MERGE_REGIONS", raw)
    assert det.conjugate_merge_regions() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "", "nope", "0.0", "none"])
def test_anything_else_merges(raw, monkeypatch):
    """A typo must not quietly stop a delivery being merged. Unrecognised means yes, on purpose:
    the merged mesh is what every delivered mesh has been, so the safe reading of a value nobody
    understands is the one that changes nothing."""
    monkeypatch.setattr(det, "CONJUGATE_MERGE_REGIONS", raw)
    assert det.conjugate_merge_regions() is True


# -- the plan carries it, the container does not re-read it --------------------------------

def test_the_plan_carries_the_decision():
    names = {f.name: f for f in dataclasses.fields(ConjugatePlan)}
    assert "merge_regions" in names, "the container is handed a workspace and nothing else"
    assert names["merge_regions"].default is True


def test_build_takes_it_and_defaults_to_merging():
    p = inspect.signature(build).parameters
    assert "merge" in p and p["merge"].default is True


# -- the chain --------------------------------------------------------------------------

def test_the_merge_is_the_one_stage_the_chain_can_leave_out():
    src = inspect.getsource(build)
    par = src.split("if int(ranks) > 1:", 1)[1].split("else:", 1)[0]
    assert "if merge else []" in par, "the merge is conditional"
    for always in ("PREPARE_PAR", '("cartesianMesh"', '("topoSet"', '("splitMeshRegions"',
                   "REGION_DICTS", "*port_stages"):
        assert always in par and "if merge" not in par.split(always, 1)[0].rsplit("(", 1)[-1], (
            f"{always} must not have become conditional too")


def test_the_per_rank_copies_are_kept_when_they_are_the_delivery():
    """Deleting them after a chain that never merged would delete the mesh itself."""
    src = inspect.getsource(build)
    assert "int(ranks) > 1 and merge" in src, (
        "the rmtree has to be guarded by the merge, not by the rank count alone")


def test_the_copies_are_still_removed_after_a_merge():
    """They are a second whole copy of the mesh - hundreds of gigabytes at the sizes that need
    ranks - and the workspace is archived and returned whole."""
    src = inspect.getsource(build)
    rm = src.split("int(ranks) > 1 and merge", 1)[1][:400]
    assert "rmtree" in rm and "processor*" in rm


# -- and the host's rule ------------------------------------------------------------------

def test_a_serial_mesh_merges_whatever_the_setting_says():
    """There are no per-rank copies to deliver, so there is nothing to not-merge."""
    from meshpipeline.engines.cfmesh_conjugate import driver

    src = inspect.getsource(driver)
    assert "True if _want <= 1 else det.conjugate_merge_regions()" in src, (
        "a one-process part must keep the exact chain every corpus part was measured with")
