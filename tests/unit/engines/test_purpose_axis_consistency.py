# Responsibility: Verify purposes and review-axis tables are one to one, typed, and declare their grounding.
from __future__ import annotations

import pytest

from meshpipeline.engines import purpose_review_axes as AX
from meshpipeline.engines.purposes import MESH_KINDS, PURPOSES, purpose_keys
from meshpipeline.engines.review_types import ReviewAxis

AXIS_TABLES = {
    "_STRUCTURAL_AXES": AX._STRUCTURAL_AXES,
    "_EXTERNAL_CFD_AXES": AX._EXTERNAL_CFD_AXES,
    "_INTERNAL_CFD_AXES": AX._INTERNAL_CFD_AXES,
    "_CHT_AXES": AX._CHT_AXES,
}


def test_every_purpose_declares_review_axes():
    bare = [k for k, p in PURPOSES.items() if not p.review_axes]
    assert not bare, f"purpose(s) {bare} declare no review axes"


def test_every_axis_table_is_attached_to_a_purpose():
    attached = {id(ax) for p in PURPOSES.values() for ax in p.review_axes}
    orphans = [name for name, table in AXIS_TABLES.items()
               if table and not any(id(ax) in attached for ax in table)]
    assert not orphans, f"axis table(s) {orphans} are attached to no purpose"


def test_the_tables_and_the_purposes_are_one_to_one():
    assert len(AXIS_TABLES) == len(PURPOSES), (
        f"{len(AXIS_TABLES)} axis tables for {len(PURPOSES)} purposes - one drifted from the other")


@pytest.mark.parametrize("key", sorted(PURPOSES))
def test_purpose_axes_are_typed_and_declare_their_grounding(key):
    for ax in PURPOSES[key].review_axes:
        assert isinstance(ax, ReviewAxis), f"{key}: {ax!r} is not a ReviewAxis"
        assert ax.name and ax.guidance, f"{key}: an axis is missing name or guidance"
        assert ax.validation_axis, f"{key}: axis {ax.name!r} declares no validation axis"
        # anti-fabrication: a render-based axis must ground itself
        assert ax.visual_only or ax.requires or ax.evidence, (
            f"{key}: axis {ax.name!r} is neither visual_only nor grounded in typed evidence")


@pytest.mark.parametrize("key", sorted(PURPOSES))
def test_axis_names_are_unique_within_a_purpose(key):
    names = [ax.name for ax in PURPOSES[key].review_axes]
    assert len(names) == len(set(names)), f"{key}: duplicate axis names {names}"


def test_every_purpose_requires_a_declared_mesh_kind():
    for key, p in PURPOSES.items():
        assert p.requires_mesh_kind, f"{key} requires no mesh kind"
        declared = ((p.requires_mesh_kind,) if isinstance(p.requires_mesh_kind, str)
                    else tuple(p.requires_mesh_kind))
        unknown = [k for k in declared if k not in MESH_KINDS]
        assert not unknown, f"{key} requires unknown mesh kind(s) {unknown}"


def test_purpose_keys_matches_the_table():
    assert set(purpose_keys()) == set(PURPOSES)


def test_the_axes_module_does_not_depend_on_the_taxonomy():
    import inspect

    src = inspect.getsource(AX)
    assert "from meshpipeline.engines.purposes import" not in src, (
        "purpose_review_axes imports the taxonomy back - the split must stay one-way")
    assert "PURPOSES" not in src, "purpose_review_axes reaches into the taxonomy table"


def test_the_rubric_composes_engine_and_purpose_axes():
    from meshpipeline.engines.quality_criteria import compose_review_rubric

    axes = compose_review_rubric("cfmesh", "external_cfd")
    owners = {ax.owner for ax in axes}
    assert any(o.startswith("engine:") for o in owners), owners
    assert any(o.startswith("purpose:") for o in owners), owners
    names = [ax.name for ax in axes]
    assert len(names) == len(set(names)), f"the union did not dedup by name: {names}"
