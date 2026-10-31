"""A region is a part you can pick, and picking it shows that region.

WHY THIS EXISTS. "Show me the coolant on its own" is the question a reader of a conjugate mesh
always asks, and the roster has listed `fluid` and `solid` since the engine was written without
being able to answer it. `renderable` was `name in rendered_set` - is one of the DRAWN SURFACES
called "fluid" - and the drawn surfaces are called "fluid/xmin" and "fluid/fluid_to_solid",
because the region prefix exists precisely so that the coolant's ymin and the metal's ymin cannot
replace one another. So the test was never true on the one engine that has regions: both rows
were listed, both were dead, and the inspector told the reader the case ships no geometry for
them.

A region's surfaces are the ones under its prefix. That is the whole fix, and it is the same
answer `drawn_by` already gives for a declared boundary.
"""
from meshpipeline.render.mesh_facts import parts


def _manifest():
    return {
        "mesh_mode": "cfmesh_conjugate",
        "mesh_units": "m",
        "cell_count": 328_714_251,
        "patch_types": {"xmin": "wall", "xmax": "wall", "ymin": "wall",
                        "ymax": "wall", "zmin": "wall", "zmax": "wall"},
        "quality": {"regions": [{"name": "fluid", "type": "fluid", "cells": 86_729_340},
                                {"name": "solid", "type": "solid", "cells": 241_984_911}]},
    }


#: what the conjugate viewer draws on the first customer plate, poor-cell sets included
DRAWN = ("fluid/xmin", "fluid/xmax", "fluid/ymax", "fluid/zmin", "fluid/zmax",
         "fluid/fluid_to_solid",
         "solid/xmin", "solid/xmax", "solid/ymin", "solid/ymax", "solid/zmin", "solid/zmax",
         "solid/concaveFaces", "solid/lowQualityTetFaces")
ENCLOSING = ("solid/xmin", "solid/xmax", "solid/ymin", "solid/ymax", "solid/zmin", "solid/zmax")
POOR = ("solid/concaveFaces", "solid/lowQualityTetFaces")


def _rows():
    return parts(_manifest(), rendered=DRAWN, enclosing=ENCLOSING, poor=POOR, list_drawn=True)


def _region(rows, name):
    return next(p for p in rows if p["id"] == f"region:{name}")


def test_the_coolant_can_be_picked():
    fluid = _region(_rows(), "fluid")
    assert fluid["renderable"] is True, "the coolant was listed and could not be selected"
    assert fluid["metadata"]["drawn_as"] == [
        "fluid/xmin", "fluid/xmax", "fluid/ymax", "fluid/zmin", "fluid/zmax",
        "fluid/fluid_to_solid"], fluid["metadata"]


def test_a_region_claims_its_own_surfaces_and_no_others():
    solid = _region(_rows(), "solid")
    assert solid["renderable"] is True
    drawn = solid["metadata"]["drawn_as"]
    assert all(s.startswith("solid/") for s in drawn), drawn
    assert not any(s.startswith("fluid/") for s in drawn), drawn


def test_a_poor_cell_set_is_not_swallowed_by_its_region():
    """A highlight is not one of the region's surfaces.

    It wears the region's prefix so the two regions' sets stay apart, but it is the boundary of
    the cells that FAILED a check, not a side of the mesh - so isolating the metal shows the
    metal, not the metal with its own red overlay welded on.
    """
    rows = _rows()
    solid = _region(rows, "solid")
    assert "solid/concaveFaces" not in solid["metadata"]["drawn_as"]
    poor = [p for p in rows if p["kind"] == "poorCells"]
    assert sorted(p["metadata"]["name"] for p in poor) == [
        "solid/concaveFaces", "solid/lowQualityTetFaces"]
    assert all(p["renderable"] for p in poor)


def test_a_region_nothing_draws_stays_un_renderable():
    """Saying so is the truthful answer; pretending to be pickable is not."""
    man = _manifest()
    man["quality"]["regions"].append({"name": "ghost", "type": "solid", "cells": 0})
    rows = parts(man, rendered=DRAWN, enclosing=ENCLOSING, poor=POOR, list_drawn=True)
    ghost = _region(rows, "ghost")
    assert ghost["renderable"] is False
    assert "drawn_as" not in ghost["metadata"]


def test_a_single_region_mesh_is_unchanged():
    """The chip: `regions: 1` with no named rows, so there is nothing to prefix-match."""
    man = {"mesh_mode": "cfmesh", "mesh_units": "m", "cell_count": 17_182_848,
           "patch_types": {"chip_zMin": "wall"},
           "quality": {"regions": 1, "cells": 17_182_848}}
    rows = parts(man, rendered=("chip_zMin",), list_drawn=True)
    regions = [p for p in rows if p["kind"] == "region"]
    assert [p["id"] for p in regions] == ["region:0"]
    assert regions[0]["renderable"] is False


def test_the_interface_is_still_a_part_of_its_own():
    """The one surface on a conjugate mesh that matters most, and the one never declared.

    `fluid_to_solid` does not exist until splitMeshRegions cuts the block, so no manifest can
    list it; it reaches the roster through the loop that lists a drawn surface no declared part
    names. That loop skips anything a declared boundary already carries - and when the coolant
    region started carrying its own surfaces, the interface was counted as carried and vanished.
    A region CONTAINS a surface; a declared boundary IS one.
    """
    rows = _rows()
    iface = [p for p in rows if p["metadata"].get("name") == "fluid/fluid_to_solid"]
    assert len(iface) == 1, [p["id"] for p in rows]
    assert iface[0]["kind"] == "boundary"
    assert iface[0]["renderable"] is True
    # and it is the coolant's, so isolating the coolant draws it
    assert "fluid/fluid_to_solid" in _region(rows, "fluid")["metadata"]["drawn_as"]


def test_every_drawn_surface_is_reachable_from_some_part():
    """Nothing the reader can see may be unpickable."""
    rows = _rows()
    reachable = set()
    for p in rows:
        if not p["renderable"]:
            continue
        reachable.update(p["metadata"].get("drawn_as") or [p["metadata"].get("name")])
    assert set(DRAWN) <= reachable, sorted(set(DRAWN) - reachable)
