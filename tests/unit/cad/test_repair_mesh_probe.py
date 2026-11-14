# Responsibility: Prove the mesh probe's error classifier names the kind and the surfaces of a gmsh failure.
# Boundaries: pure string classification; nothing here runs gmsh (see tests/native for the probe itself).
from __future__ import annotations

import pytest

from meshpipeline.cad.repair.mesh_probe import (
    EDGE_THROUGH_FACE,
    OPEN_LOOP,
    OTHER,
    OVERLAPPING_FACETS,
    PERIODIC_SURFACE,
    classify,
)


@pytest.mark.parametrize("error, kind, surfaces", [
    ("Invalid boundary mesh (overlapping facets) on surface 33 surface 33", OVERLAPPING_FACETS, (33,)),
    ("Invalid boundary mesh (overlapping facets) on surface 320 surface 324", OVERLAPPING_FACETS, (320, 324)),
    ("Impossible to mesh periodic surface 6", PERIODIC_SURFACE, (6,)),
    ("PLC Error:  A segment and a facet intersect at point (1.5,2,-3)", EDGE_THROUGH_FACE, ()),
    ("The 1D mesh seems not to be forming a closed loop", OPEN_LOOP, ()),
    ("something gmsh never said before", OTHER, ()),
])
def test_classify_names_kind_and_surfaces(error, kind, surfaces):
    failure = classify(error, [])
    assert failure.kind == kind
    assert failure.surfaces == surfaces


def test_plc_point_is_parsed():
    failure = classify("PLC Error:  A segment and a facet intersect at point (1.5,2,-3)", [])
    assert failure.point == (1.5, 2.0, -3.0)


def test_open_loop_takes_surface_from_log():
    log = ["Meshing surface 3 (Plane)", "Meshing surface 12 (Plane, Frontal-Delaunay)"]
    failure = classify("The 1D mesh seems not to be forming a closed loop", log)
    assert failure.surfaces == (12,)


def test_open_loop_without_log_has_no_surface():
    assert classify("The 1D mesh seems not to be forming a closed loop", []).surfaces == ()


def test_plc_error_takes_surface_from_log_when_it_names_none():
    log = ["Meshing surface 3 (Plane)", "Meshing surface 7 (Plane, Frontal-Delaunay)"]
    failure = classify("PLC Error:  A segment and a facet intersect at point (1,2,3)", log)
    assert failure.kind == EDGE_THROUGH_FACE
    assert failure.surfaces == (7,)


# --- Mesh failures become located defects -------------------------------------------------------

def test_faces_for_tags_matches_by_box():
    from meshpipeline.cad.repair.mesh_probe import faces_for_tags

    faces = {1: (0, 0, 0, 1, 1, 1), 2: (5, 5, 5, 6, 6, 6), 3: (-3, 0, 0, -2, 1, 1)}
    tags = {10: (5, 5, 5, 6, 6, 6), 11: (-3, 0, 0, -2, 1, 1), 12: (0, 0, 0, 1, 1, 1 + 1e-9)}
    assert faces_for_tags(faces, tags, tol=1e-6) == {10: 2, 11: 3, 12: 1}


def test_ambiguous_tag_stays_unlocated():
    from meshpipeline.cad.repair.mesh_probe import faces_for_tags

    faces = {1: (0, 0, 0, 1, 1, 1), 2: (0, 0, 0, 1, 1, 1), 3: (5, 5, 5, 6, 6, 6)}
    tags = {7: (0, 0, 0, 1, 1, 1), 8: (5, 5, 5, 6, 6, 6)}
    assert faces_for_tags(faces, tags, tol=1e-6) == {8: 3}


def test_tag_outside_tolerance_is_unmapped():
    from meshpipeline.cad.repair.mesh_probe import faces_for_tags

    faces = {1: (0, 0, 0, 1, 1, 1)}
    tags = {4: (0, 0, 0, 1, 1, 1.01), 5: (0, 0, 0, 1, 1, 1 + 5e-7)}
    assert faces_for_tags(faces, tags, tol=1e-6) == {5: 1}


def test_every_kind_has_a_code():
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.mesh_probe import (
        CODE_FOR_KIND,
        EDGE_THROUGH_FACE,
        NO_VOLUME,
        OPEN_LOOP,
        OTHER,
        OVERLAPPING_FACETS,
        PERIODIC_SURFACE,
        TIMEOUT,
    )

    kinds = {OVERLAPPING_FACETS, EDGE_THROUGH_FACE, OPEN_LOOP, PERIODIC_SURFACE, NO_VOLUME,
             TIMEOUT, OTHER}
    assert set(CODE_FOR_KIND) == kinds
    assert len(set(CODE_FOR_KIND.values())) == len(kinds)   # one-to-one
    assert all(isinstance(code, DefectCode) for code in CODE_FOR_KIND.values())
    assert CODE_FOR_KIND[OVERLAPPING_FACETS] is DefectCode.mesh_overlapping_facets
    assert CODE_FOR_KIND[TIMEOUT] is DefectCode.mesh_timeout


def test_mesh_codes_are_repairable_not_judgement():
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.triage import _NEEDS_JUDGEMENT, _REPAIRABLE

    mesh = {c.value for c in DefectCode if c.value.startswith("mesh_")}
    assert mesh == {"mesh_overlapping_facets", "mesh_edge_through_face", "mesh_open_loop",
                    "mesh_periodic_surface", "mesh_no_volume", "mesh_timeout", "mesh_other"}
    assert mesh <= _REPAIRABLE
    assert not mesh & _NEEDS_JUDGEMENT
