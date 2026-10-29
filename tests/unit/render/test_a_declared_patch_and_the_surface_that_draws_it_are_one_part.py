"""One boundary is one entry in the roster, whichever name it is wearing.

WHY THIS EXISTS. A multi-region engine draws its surfaces as "<region>/<patch>" because its two
regions own identically named block faces - both the coolant and the metal have a `ymin`, and
under the bare name one would silently replace the other in the viewer's roster. The manifest
keeps the BARE name on purpose: that is the name the user asked for at intake and the name their
boundary condition will carry.

Nothing connected the two. The declared `ymin` found no actor and was listed un-renderable;
`solid/ymin` was then appended beside it as a part of its own. Measured on the first real customer
mesh: 15 entries for 9 boundaries, with the pickable half and the drawable half being different
rows, so picking the row a reader would naturally pick did nothing at all.
"""
from meshpipeline.render.mesh_facts import parts


def _manifest():
    """A conjugate manifest: bare patch names, as delivered_boundary_types writes them."""
    return {
        "mesh_mode": "cfmesh_conjugate",
        "mesh_units": "m",
        "cell_count": 102_630_992,
        "patch_types": {"xmin": "wall", "xmax": "wall", "ymin": "wall",
                        "ymax": "wall", "zmin": "wall", "zmax": "wall"},
        "patch_face_counts": {"ymin": 145_728},
        "quality": {"regions": [{"name": "fluid", "type": "fluid", "cells": 22_062_782},
                                {"name": "solid", "type": "solid", "cells": 80_568_210}]},
    }


#: what the conjugate viewer actually draws
DRAWN = ("fluid/fluid_to_solid", "solid/xmin", "solid/xmax", "solid/ymin",
         "solid/ymax", "solid/zmin", "solid/zmax")
ENCLOSING = DRAWN[1:]          # the metal's block faces wrap everything


def _boundaries(rows):
    return [p for p in rows if p["kind"] == "boundary"]


def test_a_declared_patch_is_not_listed_twice():
    rows = parts(_manifest(), rendered=DRAWN, enclosing=ENCLOSING, list_drawn=True)
    names = [p["metadata"]["name"] for p in _boundaries(rows)]
    assert len(names) == len(set(names)), f"duplicated boundaries: {names}"
    # six declared block faces plus the interface the manifest never declares
    assert sorted(names) == ["fluid/fluid_to_solid", "xmax", "xmin",
                             "ymax", "ymin", "zmax", "zmin"], names
    assert "solid/ymin" not in names, (
        "the drawn surface is how ymin is drawn, not a second boundary beside it")


def test_the_declared_patch_is_the_one_that_is_renderable():
    """The half a reader picks and the half that draws have to be the same row."""
    rows = _boundaries(parts(_manifest(), rendered=DRAWN, enclosing=ENCLOSING, list_drawn=True))
    ymin = next(p for p in rows if p["metadata"]["name"] == "ymin")
    assert ymin["renderable"] is True
    assert ymin["metadata"]["drawn_as"] == ["solid/ymin"]
    assert ymin["metadata"]["faces"] == 145_728, "it keeps its own declared face count"


def test_a_patch_both_regions_reach_names_both_surfaces():
    """ymin legitimately exists in both regions. One declared boundary, two drawn surfaces."""
    drawn = ("fluid/ymin", "solid/ymin")
    rows = _boundaries(parts(_manifest(), rendered=drawn, list_drawn=True))
    ymin = next(p for p in rows if p["metadata"]["name"] == "ymin")
    assert ymin["metadata"]["drawn_as"] == ["fluid/ymin", "solid/ymin"]
    assert ymin["renderable"] is True
    assert "fluid/ymin" not in [p["metadata"]["name"] for p in rows]


def test_an_undeclared_drawn_surface_is_still_its_own_part():
    """The interface is written by the split, never asked for, and never declared. It must still
    be pickable or it is a wall the user cannot move out of the way."""
    rows = _boundaries(parts(_manifest(), rendered=DRAWN, enclosing=ENCLOSING, list_drawn=True))
    iface = next(p for p in rows if p["metadata"]["name"] == "fluid/fluid_to_solid")
    assert iface["renderable"] is True
    assert iface["source"] == "surface"


def test_enclosing_follows_the_surfaces_that_draw_it():
    """The metal's block faces wrap everything, so the boundaries they draw open hidden - which
    is what lets the viewer re-frame on the coolant instead of six grey rectangles."""
    rows = _boundaries(parts(_manifest(), rendered=DRAWN, enclosing=ENCLOSING, list_drawn=True))
    by = {p["metadata"]["name"]: p for p in rows}
    for n in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"):
        assert by[n]["metadata"].get("encloses_domain") is True, n
        assert by[n]["default_visibility"] == "hidden", n
    assert by["fluid/fluid_to_solid"]["default_visibility"] == "visible"


def test_half_enclosed_is_not_enclosed():
    """If one region's side wraps the domain and the other's does not, the declared boundary is
    not an enclosing one - opening it hidden would hide a surface the user needs."""
    rows = _boundaries(parts(_manifest(), rendered=("fluid/ymin", "solid/ymin"),
                             enclosing=("solid/ymin",), list_drawn=True))
    ymin = next(p for p in rows if p["metadata"]["name"] == "ymin")
    assert "encloses_domain" not in ymin["metadata"]
    assert ymin["default_visibility"] == "visible"


def test_a_single_region_engine_is_unchanged():
    """Nothing here may disturb an engine whose drawn names ARE its declared names."""
    man = {"mesh_mode": "cfmesh", "mesh_units": "m", "cell_count": 1000,
           "patch_types": {"inlet": "inlet", "outlet": "outlet", "walls": "wall"}}
    rows = _boundaries(parts(man, rendered=("inlet", "outlet", "walls"), list_drawn=True))
    assert sorted(p["metadata"]["name"] for p in rows) == ["inlet", "outlet", "walls"]
    assert all(p["renderable"] for p in rows)
    assert all("drawn_as" not in p["metadata"] for p in rows), (
        "a name that needs no translation should not carry one")
